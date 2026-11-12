"""冻结 schema 的规范编码、类型序、行摘要与排序边界树。

规则见 docs/data-contract.md：字段 ID、顺序与类型由 schema 摘要冻结；每个值带
类型标记和显式长度；字符串不做 Unicode 归一；null、缺失、空串互不相同；缺必选
字段非法。本模块只做内存中的编码、校验、比较与树结构构建。
"""
import base64
import binascii
import datetime
import hashlib
import re
from decimal import Decimal, InvalidOperation

I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1
U64_MAX = (1 << 64) - 1

TYPE_NAMES = {
    'long': 'L', 'decimal': 'D', 'string': 'S', 'bytes': 'B',
    'boolean': 'T', 'timestamp': 'U',
}
KIND_RANK = {'N': 0, 'M': 1, 'L': 2, 'D': 2, 'S': 2, 'B': 2, 'T': 2, 'U': 2}
DECIMAL_SCALE_LIMIT = 18

_DECIMAL_PATTERN = re.compile(r'[+-]?[0-9]+(?:\.[0-9]+)?\Z')
_ID_PATTERN = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
_TIME_PATTERN = re.compile(
    r'(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|\+00:00)\Z')

NULL_BYTES = b'N' + (0).to_bytes(8, 'big')
MISSING_BYTES = b'M' + (0).to_bytes(8, 'big')


def u32(v):
    if type(v) is not int or not 0 <= v <= 0xFFFFFFFF:
        raise ValueError('u32 越界')
    return v.to_bytes(4, 'big')


def u64(v):
    if type(v) is not int or not 0 <= v <= U64_MAX:
        raise ValueError('u64 越界')
    return v.to_bytes(8, 'big')


def i64(v):
    if type(v) is not int or not I64_MIN <= v <= I64_MAX:
        raise ValueError('i64 越界')
    return v.to_bytes(8, 'big', signed=True)


def fixed(tag, body):
    """类型标记 + u64 长度 + 载荷。"""
    return tag.encode('ascii') + u64(len(body)) + body


def value_tag(vbytes):
    return chr(vbytes[0])


def value_payload(vbytes):
    return memoryview(vbytes)[9:]


def value_int(vbytes):
    return int.from_bytes(vbytes[9:17], 'big', signed=True)


def canon_decimal(value, scale):
    """按声明 scale 精确转成 i64 定标整数；不可精确表示则拒绝，不隐式舍入。

    只接受十进制字符串、JSON 整数或 [定标整数, scale]；拒绝二进制浮点，因为
    JSON 浮点无法保证十进制精确。'1.2' 与 '1.20' 在 scale 2 下都是 120。
    """
    declared = int(scale)
    if isinstance(value, bool):
        raise ValueError('decimal 不接受布尔值')
    if type(value) is int:
        text = str(value)
    elif isinstance(value, float):
        raise ValueError('decimal 不接受二进制浮点，请用十进制字符串')
    elif isinstance(value, str):
        text = value
        if not _DECIMAL_PATTERN.match(text):
            raise ValueError('decimal 字符串格式非法')
    elif isinstance(value, (list, tuple)):
        if len(value) != 2 or type(value[0]) is not int or type(value[1]) is not int:
            raise ValueError('decimal 元组应为 [定标整数, scale]')
        if value[1] != declared:
            raise ValueError('decimal 元组 scale 与 schema 不一致')
        if not I64_MIN <= value[0] <= I64_MAX:
            raise ValueError('定标整数超出有符号 64 位')
        return value[0]
    else:
        raise ValueError('decimal 类型非法')
    try:
        dec = Decimal(text)
    except InvalidOperation as e:
        raise ValueError('decimal 转换失败') from e
    if not dec.is_finite() or not isinstance(dec.as_tuple().exponent, int):
        raise ValueError('decimal 必须是有限值')
    # exponent 为负 = 小数位数。声明 scale 之外的非零位拒绝；尾随零允许并规范化，
    # 因此 '1.2' 与 '1.200' 在 scale 2 下都编码为 120。
    quantum = Decimal(1).scaleb(-declared)
    if dec != dec.quantize(quantum):
        raise ValueError('小数位超出声明 scale，拒绝隐式舍入')
    dec = dec.quantize(quantum)
    unscaled = int(dec.scaleb(declared))
    if not I64_MIN <= unscaled <= I64_MAX:
        raise ValueError('定标整数超出有符号 64 位')
    return unscaled


def canon_timestamp(value):
    """UTC 微秒：接受 i64 微秒整数或带 Z/+00:00 的 ISO 文本，不做本地时区推断。"""
    if type(value) is int:
        if not I64_MIN <= value <= I64_MAX:
            raise ValueError('timestamp 微秒超出有符号 64 位')
        return value
    if not isinstance(value, str):
        raise ValueError('timestamp 必须是微秒整数或 UTC 文本')
    m = _TIME_PATTERN.match(value)
    if not m:
        raise ValueError('timestamp 文本必须是合法 UTC 时间（Z 或 +00:00）')
    year, month, day, hour, minute, second = (int(m.group(i)) for i in range(1, 7))
    micro = int((m.group(7) or '').ljust(6, '0'))
    try:
        stamp = datetime.datetime(year, month, day, hour, minute, second,
                                   micro, tzinfo=datetime.timezone.utc)
    except ValueError as e:
        raise ValueError('timestamp 文本不是合法 UTC 时间') from e
    epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    delta = stamp - epoch
    total = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    if not I64_MIN <= total <= I64_MAX:
        raise ValueError('timestamp 微秒超出有符号 64 位')
    return total


def canon_bytes(value):
    if not isinstance(value, str):
        raise ValueError('bytes 必须以 base64 字符串给出')
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as e:
        raise ValueError('bytes 不是规范 base64') from e
    if base64.b64encode(raw).decode('ascii') != value:
        raise ValueError('bytes 不是规范 base64（字符或填充不规范）')
    return raw


def value_bytes(field, value):
    """把一个 JSON 值编码成规范类型值字节。"""
    tag = TYPE_NAMES[field['type']]
    if value is None:
        return NULL_BYTES
    if tag == 'L':
        if type(value) is not int:
            raise ValueError('long 必须是有符号 64 位整数，拒绝布尔和浮点')
        return fixed('L', i64(value))
    if tag == 'D':
        return fixed('D', i64(canon_decimal(value, field['scale'])) +
                     u32(field['scale']))
    if tag == 'S':
        if not isinstance(value, str):
            raise ValueError('string 必须是字符串')
        return fixed('S', value.encode('utf8'))
    if tag == 'B':
        return fixed('B', canon_bytes(value))
    if tag == 'T':
        if type(value) is not bool:
            raise ValueError('boolean 必须是 JSON true/false')
        return fixed('T', b'\x01' if value else b'\x00')
    if tag == 'U':
        return fixed('U', i64(canon_timestamp(value)))
    raise ValueError('字段类型不受支持')


def value_ord(vbytes):
    """保序 ASCII 文本，字典序严格等于类型值序；不会因长度前缀而错序。

    null 与缺失各占一个等级且无载荷；非空值以 '2' 开头。数值把 i64 平移成无符号
    十六进制（否则负数的补码按字节就反了），字符串/bytes 用 UTF-8 原字节（
    UTF-8 字节序与码点序一致），布尔用 00/01。
    """
    rank = KIND_RANK[value_tag(vbytes)]
    if rank < 2:
        return str(rank)
    tag = value_tag(vbytes)
    if tag in ('L', 'D', 'U'):
        unsigned = value_int(vbytes) + (1 << 63)
        if tag == 'D':
            # 同字段 scale 恒定，附在数值之后即可保持定标整数序。
            return '2' + unsigned.to_bytes(8, 'big').hex() + \
                vbytes[17:21].hex() + '!'
        return '2' + unsigned.to_bytes(8, 'big').hex() + '!'
    return '2' + bytes(value_payload(vbytes)).hex() + '!'


def row_ord(values):
    return ''.join(value_ord(v) for v in values)


def key_ord(schema, values):
    return ''.join(value_ord(values[i]) for i in schema.key_index)


def bag_order(schema, values):
    """规范袋排序键：业务键类型值元组序在前，完整规范行类型值序次之。

    返回 Python 元组；两侧与分段合并都使用同一个函数，因此行序唯一且与读取
    先后无关。业务键相同的行按完整规范行（含非键列）的类型值序排列。
    """
    return (key_ord(schema, values), row_ord(values))


def type_value_cmp(a, b):
    """按 schema 类型值比较：null < 缺失 < 非空，再按类型内值。

    这是排序语义的直白实现，供测试与复核对照；正式排序走保序的 ord 文本。
    """
    ra, rb = KIND_RANK[value_tag(a)], KIND_RANK[value_tag(b)]
    if ra != rb:
        return -1 if ra < rb else 1
    if ra < 2:
        return 0
    if a[0] != b[0]:
        return -1 if a[0] < b[0] else 1
    if value_tag(a) in ('L', 'D', 'U'):
        x, y = value_int(a), value_int(b)
        return (x > y) - (x < y)
    x, y = bytes(value_payload(a)), bytes(value_payload(b))
    return (x > y) - (x < y)


def dequote(vbytes):
    """把规范类型值还原成可读值，只用于报告。不做任何格式归一。"""
    tag = value_tag(vbytes)
    body = vbytes[9:]
    if tag == 'N':
        return None
    if tag == 'M':
        return {'missing': True}
    if tag == 'L':
        return int.from_bytes(body, 'big', signed=True)
    if tag == 'D':
        return {'unscaled': int.from_bytes(body[:8], 'big', signed=True),
                'scale': int.from_bytes(body[8:12], 'big', signed=True)}
    if tag == 'S':
        return body.decode('utf8')
    if tag == 'B':
        return base64.b64encode(body).decode('ascii')
    if tag == 'T':
        return body == b'\x01'
    if tag == 'U':
        return {'utc_micros': int.from_bytes(body, 'big', signed=True)}
    raise ValueError('类型标记非法')


class Schema:
    """一次性冻结的 schema 视图。字段 ID、顺序和类型都进入摘要。"""

    def __init__(self, schema, key_fields):
        if not isinstance(schema, list) or not schema:
            raise ValueError('schema 必须是非空字段数组')
        if not isinstance(key_fields, list) or not key_fields:
            raise ValueError('key_fields 必须是非空字段数组')
        fields, seen = [], set()
        for f in schema:
            if not isinstance(f, dict):
                raise ValueError('schema 字段必须是对象')
            fid, ftype = f.get('id'), f.get('type')
            if not isinstance(fid, str) or not _ID_PATTERN.match(fid):
                raise ValueError('字段 ID 非法')
            if fid in seen:
                raise ValueError('字段 ID 重复')
            seen.add(fid)
            if ftype not in TYPE_NAMES:
                raise ValueError('字段类型不受支持')
            required = f.get('required', True)
            if type(required) is not bool:
                raise ValueError('required 必须是布尔值')
            if ftype == 'decimal':
                scale = f.get('scale')
                if type(scale) is not int or not 0 <= scale <= DECIMAL_SCALE_LIMIT:
                    raise ValueError('decimal scale 必须是非负受限整数')
            else:
                if 'scale' in f:
                    raise ValueError('只有 decimal 字段可声明 scale')
                scale = None
            bound = {'id': fid, 'type': ftype, 'required': required}
            if scale is not None:
                bound['scale'] = scale
            fields.append(bound)
        self.fields = fields
        self.field_ids = [f['id'] for f in fields]
        self.field_map = {f['id']: f for f in fields}
        self.key_fields = list(key_fields)
        for k in self.key_fields:
            if not isinstance(k, str) or k not in self.field_map:
                raise ValueError('key_fields 引用了未声明字段')
        if len(set(self.key_fields)) != len(self.key_fields):
            raise ValueError('key_fields 重复')
        self.key_index = [self.field_ids.index(k) for k in self.key_fields]
        self.key_defs = [self.field_map[k] for k in self.key_fields]
        self.digest = hashlib.sha256(self.binding_bytes()).hexdigest()

    def binding_bytes(self):
        parts = [b'bag-v1-schema\x00', u32(len(self.fields)), u32(len(self.key_fields))]
        for f in self.fields:
            parts.append(fixed('I', f['id'].encode('utf8')))
            parts.append(fixed('Y', f['type'].encode('utf8')))
            if 'scale' in f:
                parts.append(fixed('X', u32(f['scale'])))
            parts.append(b'R' if f['required'] else b'O')
        for k in self.key_fields:
            parts.append(fixed('K', k.encode('utf8')))
        return b''.join(parts)

    def binding(self):
        schema = []
        for f in self.fields:
            item = {'id': f['id'], 'type': f['type'], 'required': f['required']}
            if 'scale' in f:
                item['scale'] = f['scale']
            schema.append(item)
        return {'schema': schema, 'key_fields': list(self.key_fields)}

    def row_values(self, obj, line=None):
        """校验 JSON 行并返回按 schema 顺序的类型值字节元组。"""
        if not isinstance(obj, dict):
            raise ValueError('行必须是 JSON 对象')
        unknown = set(obj) - set(self.field_ids)
        if unknown:
            raise ValueError('行含 schema 未声明字段：%s' % ','.join(sorted(unknown)))
        values = []
        for f in self.fields:
            fid = f['id']
            if fid not in obj:
                if f['required']:
                    raise ValueError('缺少必选字段：%s（行 %s）' % (
                        fid, line if line is not None else '?'))
                values.append(MISSING_BYTES)
                continue
            try:
                values.append(value_bytes(f, obj[fid]))
            except ValueError as e:
                raise ValueError('%s：%s（行 %s）' % (
                    fid, e, line if line is not None else '?')) from e
        return tuple(values)

    def row_bytes(self, values):
        """完整规范行：字段数 u32，每字段 (ID 长度前缀, 类型值)。"""
        parts = [b'bag-v1-row\x00', u32(len(self.fields))]
        for f, value in zip(self.fields, values):
            parts.append(fixed('F', f['id'].encode('utf8')))
            parts.append(value)
        return b''.join(parts)

    def parse_row(self, raw):
        """把完整规范行解析回类型值元组，并核对冻结字段 ID、类型与 scale。"""
        if not isinstance(raw, bytes) or not raw.startswith(b'bag-v1-row\x00'):
            raise ValueError('行编码前缀不符')
        pos, end = len(b'bag-v1-row\x00'), len(raw)
        if pos + 4 > end:
            raise ValueError('行编码截断')
        count = int.from_bytes(raw[pos:pos + 4], 'big')
        pos += 4
        if count != len(self.fields):
            raise ValueError('行字段数与冻结 schema 不符')
        values = []
        for f in self.fields:
            if pos + 9 > end:
                raise ValueError('行编码截断')
            if raw[pos:pos + 1] != b'F':
                raise ValueError('行编码字段标记不符')
            idlen = int.from_bytes(raw[pos + 1:pos + 9], 'big')
            pos += 9
            if idlen > 1 << 16 or pos + idlen > end:
                raise ValueError('行编码字段 ID 非法')
            fid = raw[pos:pos + idlen].decode('utf8')
            pos += idlen
            if fid != f['id']:
                raise ValueError('行字段 ID 与冻结 schema 不符')
            if pos + 9 > end:
                raise ValueError('行编码截断')
            marker = raw[pos:pos + 1]
            length = int.from_bytes(raw[pos + 1:pos + 9], 'big')
            pos += 9
            if pos + length > end:
                raise ValueError('行编码截断')
            # 重新构造规范类型值前缀（标记 + u64 长度）与载荷。
            value = marker + length.to_bytes(8, 'big') + raw[pos:pos + length]
            try:
                tag = marker.decode('ascii', 'strict')
            except UnicodeDecodeError as e:
                raise ValueError('行字段类型标记不是 ASCII') from e
            expected = TYPE_NAMES[f['type']]
            if tag not in ('N', 'M') and tag != expected:
                raise ValueError('行字段类型与冻结 schema 不符')
            if tag == 'D':
                if length != 12:
                    raise ValueError('decimal 编码长度不符')
                if int.from_bytes(value[17:21], 'big') != f['scale']:
                    raise ValueError('decimal scale 与冻结 schema 不符')
            elif tag in ('L', 'U'):
                if length != 8:
                    raise ValueError('数值编码长度不符')
            elif tag == 'T':
                if length != 1 or value[9:10] not in (b'\x00', b'\x01'):
                    raise ValueError('boolean 编码不符')
            elif tag in ('N', 'M'):
                if length != 0:
                    raise ValueError('null/缺失编码长度不符')
            elif tag == 'S':
                try:
                    value_payload(value).tobytes().decode('utf8')
                except UnicodeDecodeError as e:
                    raise ValueError('string 不是合法 UTF-8') from e
            pos += length
            values.append(value)
        if pos != end:
            raise ValueError('行编码尾部多余字节')
        return tuple(values)

    def canonical(self, values):
        body = self.row_bytes(values)
        return {
            'key_order': key_ord(self, values),
            'row_order': row_ord(values),
            'row': body,
            'row_sha256': hashlib.sha256(body).hexdigest(),
        }

    def key_display(self, values):
        return [dequote(values[i]) for i in self.key_index]

    def row_display(self, values):
        return {f['id']: dequote(v) for f, v in zip(self.fields, values)}


def _boundary_values(schema, item, index):
    if not isinstance(item, list):
        raise ValueError('第 %d 个边界必须是 key_fields 类型值数组' % index)
    if len(item) != len(schema.key_fields):
        raise ValueError('第 %d 个边界长度必须等于 key_fields' % index)
    values = []
    for f, v in zip(schema.key_defs, item):
        try:
            values.append(value_bytes(f, v))
        except ValueError as e:
            raise ValueError('第 %d 个边界字段 %s：%s' % (index, f['id'], e)) from e
    return tuple(values)


def _boundary_display(field, value):
    try:
        return dequote(value_bytes(field, value))
    except ValueError as e:
        raise ValueError('边界值不可解码：%s' % e) from e


def boundary_key_ord(schema, item):
    """边界数组按 key_fields 声明顺序给值，不按 schema 中的字段位置。

    _boundary_values 返回的元组只含 key_fields 的值；这里直接对元组逐项取序，
    因此 schema 字段重排后同一边界数组仍得到相同键序。
    """
    values = _boundary_values(schema, item, 0)
    return ''.join(value_ord(v) for v in values)


class BoundaryTree:
    """冻结排序边界形成的左闭右开区间树。两侧共用同一棵树。

    边界由清单冻结，不根据任一侧数据推导。min/max 是独立哨兵，不与字符串同名
    混同；内部边界必须严格递增，零宽区间拒绝。叶区间可以没有行，空区间有标准
    摘要。父节点按叶层两两合并，奇数尾节点与显式 empty-child 合并，不复制末叶。
    """

    def __init__(self, schema, boundaries):
        if not isinstance(boundaries, list) or len(boundaries) < 2:
            raise ValueError('boundaries 至少需要两个端点')
        if len(boundaries) > 257:
            raise ValueError('边界过多，超出叶容量')
        first, last = boundaries[0], boundaries[-1]
        if not (isinstance(first, dict) and first.get('sentinel') == 'min'):
            raise ValueError('boundaries 必须以 min 哨兵开头')
        if not (isinstance(last, dict) and last.get('sentinel') == 'max'):
            raise ValueError('boundaries 必须以 max 哨兵结尾')
        internal_raw = boundaries[1:-1]
        for b in internal_raw:
            if isinstance(b, dict):
                raise ValueError('内部边界必须是类型值数组，不能用哨兵')
        self.schema = schema
        self.raw = boundaries
        self.internal_ord = [boundary_key_ord(schema, b) for b in internal_raw]
        for a, b in zip(self.internal_ord, self.internal_ord[1:]):
            if a >= b:
                raise ValueError('边界必须严格递增，零宽区间不允许')
        self.n_leaves = len(internal_raw) + 1
        self.structure = self._pairwise()
        self.digest = hashlib.sha256(self.binding_bytes()).hexdigest()

    def endpoint_text(self, index):
        if index == 0:
            return 'min'
        if index == self.n_leaves:
            return 'max'
        k = self.internal_ord[index - 1]
        return 'k%d:%s' % (index, k)

    def interval_text(self, lo, hi):
        return 'b[' + self.endpoint_text(lo) + '|' + self.endpoint_text(hi) + ']'

    def interval_display(self, lo, hi):
        def show(index):
            if index == 0:
                return {'sentinel': 'min'}
            if index == self.n_leaves:
                return {'sentinel': 'max'}
            items = self.raw[index]
            return [_boundary_display(f, v) for f, v in zip(self.schema.key_defs, items)]
        return {'lo': show(lo), 'hi': show(hi), 'closed_lo': True, 'closed_hi': False}

    def leaf_index(self, values):
        """按 key_fields 类型序找叶：左闭右开。"""
        ko = key_ord(self.schema, values)
        lo, hi = 0, len(self.internal_ord)
        while lo < hi:
            mid = (lo + hi) // 2
            if ko < self.internal_ord[mid]:
                hi = mid
            else:
                lo = mid + 1
        return lo

    def binding_bytes(self):
        parts = [b'bag-v1-boundary-tree\x00', bytes.fromhex(self.schema.digest),
                 u32(self.n_leaves)]
        for k in self.internal_ord:
            parts.append(fixed('B', k.encode('ascii')))
        return b''.join(parts)

    def _pairwise(self):
        """叶层两两合并；新一层奇数尾节点与显式 empty-child 合并，不复制任何节点。

        关键：前一层末尾的 empty-child 已在父节点里充当右子，提升到下一层时它是
        一个普通节点，不能再次被当成“奇数尾”而补第二个空子（那会使树有两条指向
        同一区间的分支）。只在生成新一层的分组内判断奇偶。
        """
        nodes = []
        leaves = []
        for i in range(self.n_leaves):
            node = {'id': None, 'kind': 'leaf', 'leaf': i, 'lo': i, 'hi': i + 1,
                    'children': []}
            leaves.append(node)
            nodes.append(node)
        empty_slots = []
        level = leaves
        while len(level) > 1:
            nxt = []
            for i in range(0, len(level), 2):
                left = level[i]
                if i + 1 < len(level):
                    right = level[i + 1]
                else:
                    # 本层这次分组没有右子：补一个显式 empty-child，区间零宽。
                    right = {'id': None, 'kind': 'empty', 'leaf': None,
                             'lo': left['hi'], 'hi': left['hi'], 'children': []}
                    empty_slots.append(right)
                    nodes.append(right)
                parent = {'id': None, 'kind': 'node', 'leaf': None,
                          'lo': left['lo'], 'hi': right['hi'],
                          'children': [left, right]}
                nxt.append(parent)
                nodes.append(parent)
            level = nxt
        for index, node in enumerate(nodes):
            node['id'] = index
        return {'root': level[0], 'nodes': nodes, 'leaves': leaves,
                'empty_slots': empty_slots}
