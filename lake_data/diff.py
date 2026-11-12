"""从摘要树下钻到不一致叶，并对该范围做完整有序袋差分。

根相同只说明在 SHA256 抗碰撞假设下规范多重集一致，不称绝对证明。根不同则递归
下钻，只对摘要不同的叶展开原行，逐条列出缺失、多余、同键不同值与计数差；不从
摘要猜修改前后配对。差分按完整规范行序流式归并两侧段，结论与读取顺序无关。
"""
import hashlib

from .bag import iter_segment
from .codec import key_ord
from .summary import key_ord_from_row

DOC_EMPTY = 'empty-child'


def digest_map(summary):
    return {str(item['id']): item['digest'] for item in summary['nodes']}


def mismatched_nodes(tree, left_summary, right_summary):
    """根相同返回空表；根不同则下钻到摘要不同的最细节点（叶或显式空槽）。"""
    if left_summary['root_digest'] == right_summary['root_digest']:
        return [], True
    left = digest_map(left_summary)
    right = digest_map(right_summary)
    found = []
    stack = [tree.structure['root']]
    while stack:
        node = stack.pop()
        if left.get(str(node['id'])) == right.get(str(node['id'])):
            continue
        if node['kind'] in ('leaf', 'empty'):
            found.append(node)
        else:
            stack.extend(reversed(node['children']))
    found.sort(key=lambda n: (n['lo'], n['hi']))
    return found, False


def _range_iter(tree, segment_path, lo_index, hi_index, budget):
    """流式产出落在 [lo, hi) 键区间的封存行；不重新遍历源，也不整段装内存。

    段内行按 (键序, 完整行) 单调排序，因此越过上界即可停止。
    """
    if not 0 <= lo_index <= hi_index <= tree.n_leaves:
        raise ValueError('叶区间越界')
    lo = tree.internal_ord[lo_index - 1] if lo_index > 0 else None
    # 端点 hi_index 对应第 hi_index-1 个内部边界；只有 hi_index == n_leaves 才是 max。
    hi = tree.internal_ord[hi_index - 1] if hi_index < tree.n_leaves else None
    schema = tree.schema
    for row, copies in iter_segment(segment_path, budget):
        ko = key_ord_from_row(schema, row)
        if lo is not None and ko < lo:
            continue
        if hi is not None and ko >= hi:
            break
        yield row, copies


def brief(tree, row):
    values = tree.schema.parse_row(row)
    return {
        'key': tree.schema.key_display(values),
        'row': tree.schema.row_display(values),
        'row_sha256': hashlib.sha256(row).hexdigest(),
    }


def ordered_bag_diff(tree, left_path, right_path, lo_index, hi_index, budget):
    """对叶区间做完整有序袋差分；输出按完整规范行排序。

    两侧段同时顺序读取（各占一个打开文件，受打开预算约束）。只保留有差异的条目，
    相同行即使出现偶数次也保留计数，绝不转集合或异或。每一侧的读取先后不影响
    输出顺序。
    """
    left_iter = _range_iter(tree, left_path, lo_index, hi_index, budget)
    right_iter = _range_iter(tree, right_path, lo_index, hi_index, budget)
    litem = next(left_iter, None)
    ritem = next(right_iter, None)
    entries = []
    digest = hashlib.sha256()
    digest.update(b'bag-diff-v1\n')
    left_rows = right_rows = 0
    left_distinct = right_distinct = 0
    while litem is not None or ritem is not None:
        if ritem is None or (litem is not None and litem[0] < ritem[0]):
            row, copies = litem
            kind, lc, rc = 'only_left', copies, 0
            litem = next(left_iter, None)
            left_rows += copies
            left_distinct += 1
        elif litem is None or ritem[0] < litem[0]:
            row, copies = ritem
            kind, lc, rc = 'only_right', 0, copies
            ritem = next(right_iter, None)
            right_rows += copies
            right_distinct += 1
        else:
            row = litem[0]
            lc, rc = litem[1], ritem[1]
            left_rows += lc
            right_rows += rc
            left_distinct += 1
            right_distinct += 1
            kind = 'count' if lc != rc else None
            litem = next(left_iter, None)
            ritem = next(right_iter, None)
        if kind is None:
            continue
        item = brief(tree, row)
        item.update({'kind': kind, 'left_count': lc, 'right_count': rc,
                     'difference': lc - rc})
        entries.append(item)
        digest.update(('%s\t%s\t%d\t%d\n' % (
            kind, item['row_sha256'], lc, rc)).encode('ascii'))
    return {
        'range': {
            'lo_index': lo_index,
            'hi_index': hi_index,
            'lo_text': tree.endpoint_text(lo_index),
            'hi_text': tree.endpoint_text(hi_index),
            'interval_text': tree.interval_text(lo_index, hi_index),
            'closed_lo': True,
            'closed_hi': False,
            'display': tree.interval_display(lo_index, hi_index),
        },
        'entries': entries,
        'left_rows_in_range': left_rows,
        'right_rows_in_range': right_rows,
        'left_distinct_in_range': left_distinct,
        'right_distinct_in_range': right_distinct,
        'ordered_diff_sha256': digest.hexdigest(),
        'balanced': all(e['difference'] == 0 for e in entries),
    }


def key_rollup(tree, diff):
    """把差异条目按业务键归并：缺、多、同键不同值、计数差。

    直接由有序袋差分结果派生，不再二次扫描整段；不把左右单边行配对成修改前后。
    """
    groups = {}
    order = []
    for entry in diff['entries']:
        label = tuple(_freeze(v) for v in entry['key'])
        if label not in groups:
            groups[label] = {'key': entry['key'],
                             'missing_on_right': [], 'extra_on_right': [],
                             'count_mismatch': []}
            order.append(label)
        bucket = groups[label]
        if entry['kind'] == 'only_left':
            bucket['missing_on_right'].append(entry)
        elif entry['kind'] == 'only_right':
            bucket['extra_on_right'].append(entry)
        else:
            bucket['count_mismatch'].append(entry)
    report = []
    for label in order:
        bucket = groups[label]
        status = []
        if bucket['missing_on_right']:
            status.append('missing_on_right')
        if bucket['extra_on_right']:
            status.append('extra_on_right')
        if bucket['missing_on_right'] and bucket['extra_on_right']:
            status.append('same_key_different_value')
        if bucket['count_mismatch']:
            status.append('count_mismatch')
        report.append({
            'key': bucket['key'],
            'status': status,
            'missing_on_right': bucket['missing_on_right'],
            'extra_on_right': bucket['extra_on_right'],
            'count_mismatch': bucket['count_mismatch'],
            'note': '同键不同值只列两侧各自存在的行，不推测修改前后配对',
        })
    return report


def _freeze(value):
    if isinstance(value, dict):
        return tuple(sorted((str(k), _freeze(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def counter_oracle(tree, left_path, right_path, budget):
    """小资料独立核对：完整规范行计数是否逐条相等；不用于大流正式算法。

    与摘要根不同时，能发现摘要对抗碰撞假设之外的分歧（例如哈希碰撞），因此
    不能把“根相同”直接改口成“绝对一致”。
    """
    from collections import Counter
    left = Counter()
    right = Counter()
    for row, copies in iter_segment(left_path, budget):
        left[row] += copies
    for row, copies in iter_segment(right_path, budget):
        right[row] += copies
    return {
        'equal': left == right,
        'left_rows': sum(left.values()),
        'right_rows': sum(right.values()),
        'left_distinct': len(left),
        'right_distinct': len(right),
        'symmetric_difference': len(set(left) ^ set(right)),
        'mode': 'counter',
    }
