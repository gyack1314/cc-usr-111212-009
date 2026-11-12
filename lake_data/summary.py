"""从已封存的有序袋构建叶摘要与共享边界树摘要。

叶摘要只记录 (完整行 SHA256, 计数) 流、总行数与不同编码数，按完整规范行排序。
非键列已进入完整行编码，因此也参与行摘要。父摘要按子节点覆盖区间和摘要有序
组合；奇数层末端与显式 empty-child 合并，不复制末叶。空区间有标准摘要。摘要
只由 seal 后的段计算，源未遍历完不发布根。
"""
import hashlib

from .bag import (iter_segment, leaf_digest_from_counts, leaf_stream_line,
                  parent_digest, EMPTY_CHILD_DIGEST)
from .codec import key_ord


def key_ord_from_row(schema, row):
    """从完整规范行取键序，复用 codec 的同一拼接方式，保证与冻结核一致。"""
    return key_ord(schema, schema.parse_row(row))


def build_side(tree, segment_path, budget):
    """把一个有序袋分配到冻结树；返回每叶摘要与统计。"""
    schema = tree.schema
    leaves = []
    reader = iter_segment(segment_path, budget)

    def next_row():
        try:
            return next(reader)
        except StopIteration:
            return None

    # 段已按完整行序封存，因此每叶只需顺序消费；越过上界的行留给下一叶。
    pending = next_row()
    for leaf in range(tree.n_leaves):
        lo = tree.internal_ord[leaf - 1] if leaf > 0 else None
        # 端点 leaf+1 对应第 leaf 个内部边界；只有 leaf == n_leaves-1 才是 max。
        hi = tree.internal_ord[leaf] if leaf < tree.n_leaves - 1 else None
        rows = 0
        distinct = 0
        stream = hashlib.sha256()
        while pending is not None:
            row, copies = pending
            ko = key_ord_from_row(schema, row)
            if hi is not None and ko >= hi:
                break
            if lo is not None and ko < lo:
                raise ValueError('封存段行序与冻结边界不一致')
            rows += copies
            distinct += 1
            stream.update(leaf_stream_line(row, copies).encode('ascii'))
            pending = next_row()
        lo_text = tree.endpoint_text(leaf)
        hi_text = tree.endpoint_text(leaf + 1)
        digest = leaf_digest_from_counts(
            lo_text, hi_text, rows, distinct, stream.hexdigest())
        leaves.append({
            'leaf': leaf,
            'lo_index': leaf,
            'hi_index': leaf + 1,
            'lo_text': lo_text,
            'hi_text': hi_text,
            'rows': rows,
            'distinct_encodings': distinct,
            'digest': digest,
            'stream_sha256': stream.hexdigest(),
        })
    if pending is not None:
        raise ValueError('封存段存在不属于任何冻结区间的行')
    return leaves


def summarize_tree(tree, left_leaves, right_leaves=None):
    """组合叶摘要成树。right_leaves 未给出时输出单侧摘要。"""
    digests = {}
    for leaf in tree.structure['leaves']:
        digests[leaf['id']] = left_leaves[leaf['leaf']]['digest']
    for node in tree.structure['nodes']:
        if node['kind'] in ('leaf', 'empty'):
            continue
        left, right = node['children']
        left_digest = digests[left['id']]
        right_digest = digests.get(right['id'], EMPTY_CHILD_DIGEST)
        digests[node['id']] = parent_digest(
            tree.endpoint_text(node['lo']), tree.endpoint_text(node['hi']),
            left_digest, right_digest)
    serial = []
    for node in sorted(tree.structure['nodes'], key=lambda n: n['id']):
        item = {
            'id': node['id'],
            'kind': node['kind'],
            'lo_index': node['lo'],
            'hi_index': node['hi'],
            'coverage': {
                'lo_index': node['lo'],
                'hi_index': node['hi'],
                'lo_text': tree.endpoint_text(node['lo']),
                'hi_text': tree.endpoint_text(node['hi']),
                'interval_text': tree.interval_text(node['lo'], node['hi']),
                'left_closed': True,
                'right_open': True,
                'display': tree.interval_display(node['lo'], node['hi']),
            },
            'digest': digests.get(node['id'], EMPTY_CHILD_DIGEST),
            'child_ids': [c['id'] for c in node['children']],
        }
        if node['kind'] == 'leaf':
            info = left_leaves[node['leaf']]
            item['rows'] = info['rows']
            item['distinct_encodings'] = info['distinct_encodings']
            if right_leaves is not None:
                item['right_rows'] = right_leaves[node['leaf']]['rows']
                item['right_distinct_encodings'] = right_leaves[node['leaf']][
                    'distinct_encodings']
        serial.append(item)
    root = tree.structure['root']
    return {
        'root_digest': digests[root['id']],
        'root_coverage': tree.interval_display(0, tree.n_leaves),
        'nodes': serial,
        'leaf_digests': {str(i): left_leaves[i]['digest']
                         for i in range(tree.n_leaves)},
    }


def verify_node(node, child_digests, tree):
    """独立重算一个父摘要。"""
    left, right = node['children']
    return parent_digest(
        tree.endpoint_text(node['lo']), tree.endpoint_text(node['hi']),
        child_digests[left['id']],
        child_digests.get(right['id'], EMPTY_CHILD_DIGEST))
