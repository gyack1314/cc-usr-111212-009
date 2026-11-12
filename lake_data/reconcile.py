"""跨分区对账：单次摄取、冻结边界摘要树、范围内有序袋差分。

约束：
- 两侧源各自只遍历一次；摄取阶段流式记录实际行数与字节数，并与清单核对。
- 索引绑定快照身份、schema 摘要、编码版本与边界树摘要；任一不符拒绝。
- 源未完整遍历、或清单的行数/字节数/SHA256 不符，就不发布根摘要。
- 摘要与差分只读封存段；只对摘要不一致的叶展开原行。
- 打开文件数与内存条目预算有硬上限；大重复组外排合并。
- 根相同仅表示在 SHA256 抗碰撞假设下规范多重集一致，不称绝对证明。
"""
import hashlib
import json
import os
import shutil
import tempfile
import time

from .bag import OpenBudget, segment_sha256, SegmentBag
from .codec import Schema, BoundaryTree
from .diff import mismatched_nodes, ordered_bag_diff
from .diff import key_rollup, counter_oracle
from .io import load
from .summary import build_side, summarize_tree
from .codec import bag_order


ENCODING_VERSION = 'bag-v1'
DOMAIN_TAG = 'bag-v1'


class ReconcileError(ValueError):
    """清单、源流、快照或索引不一致；不发布摘要。"""


def _require(condition, message):
    if not condition:
        raise ReconcileError(message)


def resolve_source(manifest, root, side):
    """解析源路径并核对清单声明的字节数与 SHA256，只读取一次文件内容。

    返回 (path, declared)。SHA256 在摄取时边读边算；此处只核对字节数，避免多读
    一遍源。路径越界直接拒绝。
    """
    ref = manifest.get(side)
    if not isinstance(ref, dict):
        raise ReconcileError('%s 清单缺失' % side)
    path = ref.get('path')
    if not isinstance(path, str) or not path or os.path.isabs(path):
        raise ReconcileError('%s 源路径非法' % side)
    base = os.path.abspath(root)
    full = os.path.abspath(os.path.join(base, path))
    if os.path.commonpath([base, full]) != base:
        raise ReconcileError('%s 源路径越界' % side)
    declared_bytes = ref.get('bytes')
    declared_rows = ref.get('rows')
    if type(declared_bytes) is not int or declared_bytes < 0:
        raise ReconcileError('%s 声明字节数非法' % side)
    if type(declared_rows) is not int or declared_rows < 0:
        raise ReconcileError('%s 声明行数非法' % side)
    if not os.path.isfile(full):
        raise ReconcileError('%s 源文件不存在' % side)
    if os.path.getsize(full) != declared_bytes:
        raise ReconcileError('%s 源字节数与清单不符' % side)
    return full, ref


def ingest(tree, source_path, bag, declared):
    """单次遍历一个源：逐行校验、编码、入袋；返回实际统计与源摘要。

    顺带流式重算源 SHA256 与字节数，与清单核对；源在读期间被改动会在此失败。
    """
    schema = tree.schema
    budget = bag.budget
    rows = 0
    raw_bytes = 0
    source_hash = hashlib.sha256()
    # 单次打开、顺序读取；同一行只解析一次。行号从 1 起用于定位错误。
    with budget.file(source_path, 'rb') as f:
        for line_no, line in enumerate(f, 1):
            source_hash.update(line)
            raw_bytes += len(line)
            if not line.strip():
                raise ReconcileError('源含空行：第 %d 行' % line_no)
            try:
                obj = json.loads(line.decode('utf8'))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise ReconcileError('源行不是合法 JSON：第 %d 行' % line_no) from e
            if not isinstance(obj, dict):
                raise ReconcileError('源行必须是 JSON 对象：第 %d 行' % line_no)
            try:
                values = schema.row_values(obj, line_no)
            except ValueError as e:
                raise ReconcileError('源行非法：%s（第 %d 行）' % (e, line_no)) from e
            bag.add(schema.canonical(values)['row'])
            rows += 1
    return {
        'path': source_path,
        'rows_read': rows,
        'bytes_read': raw_bytes,
        'source_sha256': source_hash.hexdigest(),
        'declared_rows': declared['rows'],
        'declared_bytes': declared['bytes'],
    }


def check_source(manifest, stats, side):
    ref = manifest[side]
    _require(stats['bytes_read'] == ref['bytes'],
             '%s 实际读取字节与清单不符：%d != %d' % (
                 side, stats['bytes_read'], ref['bytes']))
    _require(stats['rows_read'] == ref['rows'],
             '%s 实际行数与清单不符：%d != %d' % (
                 side, stats['rows_read'], ref['rows']))
    expected = ref.get('sha256')
    if not isinstance(expected, str) or len(expected) != 64:
        raise ReconcileError('%s 清单缺少合法 SHA256' % side)
    _require(stats['source_sha256'] == expected.lower(),
             '%s 源 SHA256 与清单不符' % side)


def build_tree(manifest):
    """冻结 schema 与边界树；边界只看清单，不看任一侧数据。"""
    try:
        schema = Schema(manifest['schema'], manifest['key_fields'])
    except (ValueError, KeyError, TypeError) as e:
        raise ReconcileError('schema 冻结失败：%s' % e) from e
    try:
        tree = BoundaryTree(schema, manifest['boundaries'])
    except (ValueError, KeyError, TypeError) as e:
        raise ReconcileError('边界树冻结失败：%s' % e) from e
    return tree


def index_binding(manifest, tree, stats, segments):
    """索引绑定：快照、schema、编码版本、边界树与两侧封存段摘要。"""
    for key in ('snapshot_left', 'snapshot_right'):
        value = manifest.get(key)
        if not isinstance(value, str) or not value:
            raise ReconcileError('索引缺少快照标识：%s' % key)
    return {
        'encoding_version': ENCODING_VERSION,
        'schema_sha256': tree.schema.digest,
        'boundary_tree_sha256': tree.digest,
        'leaf_count': tree.n_leaves,
        'snapshot_left': manifest['snapshot_left'],
        'snapshot_right': manifest['snapshot_right'],
        'left_source_sha256': stats['left']['source_sha256'],
        'right_source_sha256': stats['right']['source_sha256'],
        'left_segment_sha256': stats['left']['segment_sha256'],
        'right_segment_sha256': stats['right']['segment_sha256'],
    }


def row_order_key(tree, row):
    """规范袋排序键：(业务键类型序元组, 完整规范行类型序)。

    两者都按类型值序，不使用长度前缀字节序；同键不同全行落在同一键区间内，按
    非键列与键列共同的完整行序区分。段内与合并都使用这一键。
    """
    return bag_order(tree.schema, tree.schema.parse_row(row))


def reconcile(manifest_path, workdir=None, keep=False):
    """对账入口。根摘要只在两侧源完整读取并核对后发布。"""
    started = time.monotonic()
    manifest, manifest_digest = load(manifest_path)
    root = os.path.dirname(os.path.abspath(manifest_path))
    version = manifest.get('encoding_version')
    _require(version == ENCODING_VERSION,
             'manifest.encoding_version 必须为 %s，得到 %r' % (
                 ENCODING_VERSION, version))
    memory_bytes = manifest.get('memory_bytes')
    max_open = manifest.get('max_open_files')
    if type(memory_bytes) is not int or memory_bytes <= 0:
        raise ReconcileError('memory_bytes 必须为正整数')
    # 至少需要：摄取时源 1 + 段 1；差分时左右段各 1 + 段校验的内部迭代。
    if type(max_open) is not int or max_open < 3:
        raise ReconcileError('max_open_files 必须至少为 3')
    tree = build_tree(manifest)
    # 条目预算按声明内存估算；每条约 256 字节，留出解释器开销的余量。
    entry_budget = max(1, memory_bytes // 256)
    budget = OpenBudget(max_open)
    own_dir = tempfile.mkdtemp(prefix='bagrec-', dir=workdir)
    try:
        bags, stats, segments = {}, {}, {}
        for side in ('left', 'right'):
            side_dir = os.path.join(own_dir, side)
            os.makedirs(side_dir, exist_ok=True)
            bags[side] = SegmentBag(
                side_dir, entry_budget, budget,
                order_key=lambda row: row_order_key(tree, row))
            source_path, declared = resolve_source(manifest, root, side)
            # 摄取期间源文件占一个打开名额；封印前源已关闭。
            bags[side].source_open = True
            stats[side] = ingest(tree, source_path, bags[side], declared)
            bags[side].source_open = False
            check_source(manifest, stats[side], side)
            segments[side] = bags[side].seal('%s.seg' % side)
            stats[side]['segment_sha256'] = segment_sha256(segments[side])
            stats[side]['segments_written'] = bags[side].runs
            stats[side]['peak_live_segments'] = bags[side].peak_live
        # 两侧源均已完整遍历并通过身份核对，才发布根摘要。
        left_leaves = build_side(tree, segments['left'], budget)
        right_leaves = build_side(tree, segments['right'], budget)
        left_summary = summarize_tree(tree, left_leaves)
        right_summary = summarize_tree(tree, right_leaves)
        binding = index_binding(manifest, tree, stats, segments)
        mismatched, roots_equal = mismatched_nodes(
            tree, left_summary, right_summary)
        drills = []
        for node in mismatched:
            # 只展开摘要不一致的叶或空槽；整段按完整行序做袋差分。
            section = ordered_bag_diff(
                tree, segments['left'], segments['right'],
                node['lo'], node['hi'], budget)
            section['node_ids'] = [node['id']]
            section['node_kind'] = node['kind']
            section['key_rollup'] = key_rollup(tree, section)
            drills.append(section)
        if roots_equal:
            # 根相同：小资料用 Counter 独立核对；大资料也只按对比上限决定。
            oracle = counter_oracle(tree, segments['left'], segments['right'],
                                    budget)
            expanded = 'none'
        else:
            oracle = {
                'mode': 'bounded',
                'expanded_leaf_ranges': len(drills),
                'note': '根不同，只对不一致叶展开原行；未对全部行做 Counter',
            }
            expanded = 'mismatched_leaves_only'
        report = {
            'domain_tag': DOMAIN_TAG,
            'encoding_version': ENCODING_VERSION,
            'manifest_sha256': manifest_digest,
            'index_binding': binding,
            'schema': tree.schema.binding(),
            'schema_sha256': tree.schema.digest,
            'boundaries': {
                'digest': tree.digest,
                'leaf_count': tree.n_leaves,
                'shared_between_sides': True,
                'derived_from_data': False,
                'display': tree.interval_display(0, tree.n_leaves),
            },
            'summary': {
                'left': left_summary,
                'right': right_summary,
                'roots_equal': roots_equal,
                'equality_claim': (
                    '在 SHA256 抗碰撞假设下规范多重集一致；不是数学上的绝对证明'
                    if roots_equal else
                    '摘要不同，规范多重集不一致；差异见下钻范围与袋差分'),
            },
            'drills': drills,
            'expansion': expanded,
            'oracle': oracle,
            'read_stats': {
                'left': {k: v for k, v in stats['left'].items()
                         if k != 'segment_sha256'},
                'right': {k: v for k, v in stats['right'].items()
                          if k != 'segment_sha256'},
                'peak_buffered_entries': max(bags['left'].peak_buffered,
                                             bags['right'].peak_buffered),
                'entry_budget': entry_budget,
                'memory_bytes': memory_bytes,
                'max_open_files': max_open,
                'peak_open_files': budget.peak,
                'segments_written': bags['left'].runs + bags['right'].runs,
                'elapsed_seconds': round(time.monotonic() - started, 6),
            },
            'segments': {
                'left': segments['left'],
                'right': segments['right'],
            },
            'workdir': own_dir,
        }
        return _finalize(report, keep, own_dir)
    finally:
        if not keep:
            shutil.rmtree(own_dir, ignore_errors=True)


def _finalize(report, keep, own_dir):
    if not keep:
        report.pop('segments', None)
        report.pop('workdir', None)
    return report


def canonical_json(report):
    return json.dumps(report, ensure_ascii=False, indent=2,
                      allow_nan=False)
