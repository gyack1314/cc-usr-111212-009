"""一次性行流的外排排序，保留重复重数；叶摘要与层级摘要。

每侧源只遍历一次：行校验、编码后按 (键类型序, 完整行编码) 压进有序袋。内存按
条目预算封存成段，段数超过打开上限就先合并；大重复组也走外排，不把全部键留在
内存。摘要与差分只读封存段，不回读源。

段是 ASCII 文本，逐行可复核：

    BAGSEG4
    <计数>\\t<行SHA256>\\t<完整规范行十六进制>
    ...
    END\\t<条目数>\\t<段体SHA256>

读取时流式校验条目数、逐行长度与段体 SHA256；损坏段一律报错，绝不当作空区间。
"""
import hashlib
import heapq
import os
from contextlib import contextmanager

MAGIC = b'BAGSEG4'
MAGIC_LINE = MAGIC + b'\n'
END_PREFIX = b'END\t'


class BagError(ValueError):
    """封存段损坏或排序约定被破坏。"""


class OpenBudget:
    """统计并限制同时打开的文件数；超限立刻失败而不是悄悄超出。

    计数是理论占用，不是 os 级文件描述符的实时快照：生成器未消费完时可能比
    实际更保守，这只会提前触发合并，不会突破上限。
    """

    def __init__(self, limit):
        self.limit = int(limit)
        self.open = 0
        self.peak = 0

    @contextmanager
    def file(self, path, mode='rb'):
        if self.open + 1 > self.limit:
            raise BagError('打开文件数超过上限 %d' % self.limit)
        self.open += 1
        self.peak = max(self.peak, self.open)
        try:
            with open(path, mode) as handle:
                yield handle
        finally:
            self.open -= 1


def segment_sha256(path):
    with open(path, 'rb') as f:
        digest = hashlib.sha256()
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def iter_segment(path, budget):
    """流式读取并校验段，逐条产出 (完整规范行 bytes, 计数)。

    迭代必须走到 END 才完成校验；中途丢弃迭代器会关闭文件，但不会发布摘要——
    摘要路径总是消费到结尾，或由调用方显式 close。
    """
    with budget.file(path, 'rb') as f:
        first = f.readline()
        if first != MAGIC_LINE:
            raise BagError('段损坏：魔数不符')
        digest = hashlib.sha256()
        count = 0
        for line in f:
            if line.startswith(END_PREFIX):
                parts = line.rstrip(b'\n').split(b'\t')
                if len(parts) != 3:
                    raise BagError('段损坏：结束行非法')
                try:
                    declared = int(parts[1])
                except ValueError as e:
                    raise BagError('段损坏：结束行条目数非法') from e
                if declared != count:
                    raise BagError('段损坏：条目数与结束行不符')
                if digest.hexdigest().encode('ascii') != parts[2]:
                    raise BagError('段损坏：段体 SHA256 不符')
                if f.readline() != b'':
                    raise BagError('段损坏：结束行后有多余内容')
                return
            if not line.endswith(b'\n'):
                raise BagError('段损坏：条目缺少换行')
            parts = line[:-1].split(b'\t')
            if len(parts) != 3:
                raise BagError('段损坏：条目字段数不符')
            try:
                copies = int(parts[0])
                row = bytes.fromhex(parts[2].decode('ascii'))
            except ValueError as e:
                raise BagError('段损坏：条目字段非法') from e
            if copies <= 0 or not row:
                raise BagError('段损坏：空行或零计数')
            expected = hashlib.sha256(row).hexdigest().encode('ascii')
            if expected != parts[1]:
                raise BagError('段损坏：行 SHA256 不符')
            digest.update(line)
            count += 1
            yield row, copies
        raise BagError('段损坏：缺少结束行')


def write_segment(path, entries, budget):
    """把有序条目流原子写出；entries 是 (完整规范行, 计数) 迭代器。"""
    tmp = path + '.tmp'
    try:
        with budget.file(tmp, 'wb') as f:
            f.write(MAGIC_LINE)
            digest = hashlib.sha256()
            count = 0
            for row, copies in entries:
                if copies <= 0 or not row:
                    raise BagError('拒绝写出空行或零计数')
                line = b'%d\t%s\t%s\n' % (
                    copies, hashlib.sha256(row).hexdigest().encode('ascii'),
                    row.hex().encode('ascii'))
                f.write(line)
                digest.update(line)
                count += 1
            f.write(END_PREFIX + str(count).encode('ascii') + b'\t' +
                    digest.hexdigest().encode('ascii') + b'\n')
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def merge_segments(paths, target, order_key, budget):
    """多路合并有序段；相同完整行累加计数，偶数次重复不会消失。

    同时打开 len(paths) 个输入段和 1 个输出段；调用点负责让总数不超预算。
    """
    iterators = [iter_segment(p, budget) for p in paths]
    heap = []
    for index, iterator in enumerate(iterators):
        try:
            row, copies = next(iterator)
        except StopIteration:
            continue
        heapq.heappush(heap, (order_key(row), index, row, copies))

    def merged():
        last, last_count = None, 0
        while heap:
            _, index, row, copies = heapq.heappop(heap)
            if row == last:
                last_count += copies
            else:
                if last is not None:
                    yield last, last_count
                last, last_count = row, copies
            try:
                nxt_row, nxt_copies = next(iterators[index])
            except StopIteration:
                continue
            heapq.heappush(heap, (order_key(nxt_row), index, nxt_row, nxt_copies))
        if last is not None:
            yield last, last_count

    write_segment(target, merged(), budget)


def leaf_stream_line(row, copies):
    return '%s:%d\n' % (hashlib.sha256(row).hexdigest(), copies)


def leaf_digest_from_counts(lo_text, hi_text, rows, distinct, stream_digest):
    """叶摘要：domain-tag、左闭右开边界、(行SHA256,计数) 流摘要、总行数、不同编码数。"""
    body = ('bag-leaf-v3\nlo=%s\nhi=%s\nrows=%d\ndistinct=%d\nstream\n'
            'stream-sha256=%s\nend\n' % (
                lo_text, hi_text, rows, distinct, stream_digest))
    return hashlib.sha256(body.encode('ascii')).hexdigest()


def parent_digest(lo_text, hi_text, left_digest, right_digest):
    """父摘要：domain-tag、完整覆盖区间、左子摘要、右子摘要（或 empty-child）。"""
    body = ('bag-parent-v3\nlo=%s\nhi=%s\nleft=%s\nright=%s\n' % (
        lo_text, hi_text, left_digest, right_digest))
    return hashlib.sha256(body.encode('ascii')).hexdigest()


EMPTY_CHILD_DIGEST = 'empty-child'


class SegmentBag:
    """把单侧流压成按 (键序, 完整行) 排序的 (行, 计数) 袋。"""

    def __init__(self, workdir, entry_budget, budget, order_key):
        self.workdir = workdir
        self.entry_budget = max(1, int(entry_budget))
        self.budget = budget
        self.order_key = order_key
        self.buffer = {}
        self.buffered = 0
        self.runs = 0
        self.live = []
        self.peak_buffered = 0
        self.peak_live = 0
        self.source_open = False

    def add(self, row):
        existing = self.buffer.get(row)
        if existing is None:
            self.buffer[row] = 1
            self.buffered += 1
            self.peak_buffered = max(self.peak_buffered, self.buffered)
            if self.buffered >= self.entry_budget:
                self.flush()
        else:
            self.buffer[row] = existing + 1

    def flush(self):
        if not self.buffer:
            return
        items = sorted(self.buffer.items(), key=lambda kv: self.order_key(kv[0]))
        self.buffer = {}
        self.buffered = 0
        path = os.path.join(self.workdir, 'run-%05d.seg' % self.runs)
        self.runs += 1
        write_segment(path, items, self.budget)
        self.live.append(path)
        self.peak_live = max(self.peak_live, len(self.live))
        self._compact()

    def _max_inputs(self):
        # 合并需要“输入段数 + 1 路输出”同时打开；摄取时源文件也占一路。
        # 若剩余名额不足以形成 2 路输入，就暂不合并；未打开的段只占磁盘，不占
        # 打开文件数，等到 seal（源已关闭）再统一合并。
        reserve = 1 + (1 if self.source_open else 0)
        return self.budget.limit - reserve

    def _merge_live(self):
        while len(self.live) > self._max_inputs() >= 2:
            fan_in = self._max_inputs()
            group, rest = self.live[:fan_in], self.live[fan_in:]
            target = os.path.join(self.workdir, 'merge-%05d.seg' % self.runs)
            self.runs += 1
            merge_segments(group, target, self.order_key, self.budget)
            for path in group:
                os.unlink(path)
            self.live = [target] + rest

    _compact = _merge_live

    def seal(self, name='bag.seg'):
        """源遍历完且身份核对通过后才调用；返回最终有序段。"""
        # 源已关闭，封印阶段多出的一路名额可用于合并。
        self.source_open = False
        self.flush()
        while len(self.live) > 1:
            fan_in = self._max_inputs()
            if fan_in < 1:
                raise BagError('max_open_files 太小，无法完成合并')
            group, rest = self.live[:fan_in], self.live[fan_in:]
            target = os.path.join(self.workdir, 'seal-%05d.seg' % self.runs)
            self.runs += 1
            merge_segments(group, target, self.order_key, self.budget)
            for path in group:
                os.unlink(path)
            self.live = [target] + rest
        if not self.live:
            path = os.path.join(self.workdir, name)
            write_segment(path, [], self.budget)
            self.live = [path]
        return self.live[0]
