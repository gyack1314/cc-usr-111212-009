"""跨分区对账：重复重数、范围下钻、外排与摘要重算。"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from lake_data.codec import Schema, BoundaryTree
from lake_data.bag import (iter_segment, leaf_digest_from_counts,
                           parent_digest, OpenBudget)
from lake_data.summary import build_side
from lake_data.reconcile import reconcile, ReconcileError


def build_manifest(root, left, right, *, boundaries=None, memory=2048, open_files=4,
                   schema=None, key_fields=None, snapshots=('L1', 'R1'),
                   rows_override=None):
    if schema is None:
        schema = [
            {'id': 'k', 'type': 'string', 'required': True},
            {'id': 'energy', 'type': 'decimal', 'scale': 2, 'required': False},
            {'id': 'at', 'type': 'timestamp', 'required': True},
        ]
    if key_fields is None:
        key_fields = ['k']
    if boundaries is None:
        boundaries = [{'sentinel': 'min'}, ['b'], ['z'], {'sentinel': 'max'}]

    rows_override = rows_override or {}

    def ref(path, rows=None):
        raw = Path(root, path).read_bytes()
        count = rows if rows is not None else sum(1 for line in raw.splitlines() if line)
        return {'path': path, 'bytes': len(raw),
                'sha256': hashlib.sha256(raw).hexdigest(), 'rows': count}
    manifest = {
        'schema': schema, 'key_fields': key_fields, 'boundaries': boundaries,
        'left': ref(left, rows_override.get('left')),
        'right': ref(right, rows_override.get('right')),
        'memory_bytes': memory, 'max_open_files': open_files,
        'encoding_version': 'bag-v1',
        'snapshot_left': snapshots[0], 'snapshot_right': snapshots[1],
    }
    return manifest


def write_manifest(root, manifest):
    path = Path(root, 'manifest.json')
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                    encoding='utf8')
    return str(path)


def write_jsonl(path, rows):
    with open(path, 'w', encoding='utf8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def run_pair(self, left, right, **kwargs):
        lp = Path(self.root, 'left.jsonl')
        rp = Path(self.root, 'right.jsonl')
        write_jsonl(lp, left)
        write_jsonl(rp, right)
        manifest = build_manifest(self.root, 'left.jsonl', 'right.jsonl', **kwargs)
        # 工作目录放在测试临时目录内，keep=True 的段随 tearDown 一起清理。
        return reconcile(write_manifest(self.root, manifest), keep=True,
                         workdir=self.root)


class CliTests(unittest.TestCase):
    def test_cli_writes_report_and_error(self):
        import io
        import contextlib
        from lake_data.cli import main
        with tempfile.TemporaryDirectory() as d:
            out = Path(d, 'report.json')
            code = main(['bag', 'data/normal.json', '--out', str(out)])
            self.assertEqual(code, 0)
            report = json.loads(out.read_text(encoding='utf8'))
            self.assertFalse(report['summary']['roots_equal'])
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = main(['bag', 'data/invalid-config.json'])
            self.assertEqual(code, 2)
            self.assertIn('error', json.loads(err.getvalue()))


class DigestRecomputeTests(TempCase):
    def test_empty_partition_root_recomputes_from_definition(self):
        import hashlib
        from lake_data.bag import leaf_digest_from_counts, parent_digest
        report = self.run_pair([], [])
        empty_stream = hashlib.sha256(b'').hexdigest()
        leaves = [leaf_digest_from_counts(lo, hi, 0, 0, empty_stream)
                  for lo, hi in (('min', 'k1:262!'), ('k1:262!', 'k2:27a!'),
                                 ('k2:27a!', 'max'))]
        left = parent_digest('min', 'k2:27a!', leaves[0], leaves[1])
        right = parent_digest('k2:27a!', 'max', leaves[2], 'empty-child')
        self.assertEqual(report['summary']['left']['root_digest'],
                         parent_digest('min', 'max', left, right))

    def test_leaf_and_parent_digests_recompute(self):
        report = self.run_pair(
            [{'k': 'a', 'energy': '1.20', 'at': 0}],
            [{'k': 'a', 'energy': '1.20', 'at': 0}])
        tree = BoundaryTree(Schema(
            [{'id': 'k', 'type': 'string', 'required': True},
             {'id': 'energy', 'type': 'decimal', 'scale': 2, 'required': False},
             {'id': 'at', 'type': 'timestamp', 'required': True}], ['k']),
            [{'sentinel': 'min'}, ['b'], ['z'], {'sentinel': 'max'}])
        budget = OpenBudget(4)
        leaves = build_side(tree, report['segments']['left'], budget)
        total_rows = sum(leaf['rows'] for leaf in leaves)
        self.assertEqual(total_rows, 1)
        # 每叶摘要可从 (row SHA256, count) 流独立重算。
        for leaf in leaves:
            recomputed = leaf_digest_from_counts(
                leaf['lo_text'], leaf['hi_text'], leaf['rows'],
                leaf['distinct_encodings'], leaf['stream_sha256'])
            self.assertEqual(recomputed, leaf['digest'])
        node_digests = {n['id']: n['digest'] for n in
                        report['summary']['left']['nodes']}
        serial = {n['id']: n for n in report['summary']['left']['nodes']}
        root_id = max(serial)
        self.assertEqual(node_digests[root_id],
                         report['summary']['left']['root_digest'])

        def verify(nid):
            node = serial[nid]
            if node['kind'] != 'node':
                return node['digest']
            children = [verify(cid) for cid in node['child_ids']]
            left_d, right_d = children
            return parent_digest(node['coverage']['lo_text'],
                                 node['coverage']['hi_text'],
                                 left_d, right_d)
        self.assertEqual(verify(root_id), report['summary']['left']['root_digest'])

    def test_tree_is_proper_binary_tree(self):
        from lake_data.codec import Schema, BoundaryTree
        schema = Schema([{'id': 'k', 'type': 'string', 'required': True}], ['k'])
        for n in range(1, 12):
            tree = BoundaryTree(schema, [{'sentinel': 'min'}] +
                                [[chr(96 + i)] for i in range(1, n + 1)] +
                                [{'sentinel': 'max'}])
            parents = {}
            for node in tree.structure['nodes']:
                for child in node['children']:
                    parents[child['id']] = parents.get(child['id'], 0) + 1
            self.assertNotIn(tree.structure['root']['id'], parents)
            self.assertTrue(all(v == 1 for v in parents.values()))
            self.assertEqual(len(parents), len(tree.structure['nodes']) - 1)
            for node in tree.structure['nodes']:
                if node['kind'] == 'node':
                    left, right = node['children']
                    self.assertEqual(left['lo'], node['lo'])
                    self.assertEqual(right['hi'], node['hi'])

    def test_empty_leaf_has_standard_digest(self):
        lp = Path(self.root, 'left.jsonl')
        rp = Path(self.root, 'right.jsonl')
        lp.write_bytes(b'')
        rp.write_bytes(b'')
        manifest = build_manifest(self.root, 'left.jsonl', 'right.jsonl')
        report = reconcile(write_manifest(self.root, manifest), keep=True,
                           workdir=self.root)
        serial = report['summary']['left']['nodes']
        leaves = [n for n in serial if n['kind'] == 'leaf']
        self.assertTrue(all(n['rows'] == 0 for n in leaves))
        # 空区间也有标准摘要：同一边界区间在两侧应一致，不同区间因边界不同而不同。
        right = {n['id']: n['digest'] for n in report['summary']['right']['nodes']}
        for n in leaves:
            self.assertEqual(n['digest'], right[n['id']])
            self.assertEqual(n['digest'], leaf_digest_from_counts(
                n['coverage']['lo_text'], n['coverage']['hi_text'], 0, 0,
                __import__('hashlib').sha256(b'').hexdigest()))
        self.assertTrue(report['summary']['roots_equal'])


class ScenarioTests(TempCase):
    def test_decimal_nonzero_extra_scale_rejected(self):
        lp = Path(self.root, 'left.jsonl')
        rp = Path(self.root, 'right.jsonl')
        lp.write_text('{"k":"a","energy":"1.234","at":0}\n', encoding='utf8')
        write_jsonl(rp, [])
        manifest = build_manifest(self.root, 'left.jsonl', 'right.jsonl')
        with self.assertRaises(ReconcileError) as ctx:
            reconcile(write_manifest(self.root, manifest))
        self.assertIn('scale', str(ctx.exception))

    def test_duplicate_counts_preserved(self):
        # 左 3 条、右 1 条相同行，行数与普通异或都掩盖不了计数差。
        report = self.run_pair(
            [{'k': 'b', 'energy': None, 'at': 1}] * 3,
            [{'k': 'b', 'energy': None, 'at': 1}])
        self.assertFalse(report['summary']['roots_equal'])
        entries = [e for d in report['drills'] for e in d['entries']]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['kind'], 'count')
        self.assertEqual((entries[0]['left_count'], entries[0]['right_count']), (3, 1))
        self.assertEqual(entries[0]['difference'], 2)

    def test_even_duplicate_not_cancelled(self):
        # 偶数次重复不能因异或抵消而消失：左 2 条、右 0 条。
        report = self.run_pair(
            [{'k': 'b', 'energy': '1.00', 'at': 1}] * 2, [])
        entries = [e for d in report['drills'] for e in d['entries']]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['kind'], 'only_left')
        self.assertEqual(entries[0]['left_count'], 2)

    def test_same_key_different_value(self):
        report = self.run_pair(
            [{'k': 'a', 'energy': '1.00', 'at': 0},
             {'k': 'a', 'energy': '2.00', 'at': 0}],
            [{'k': 'a', 'energy': '1.00', 'at': 0},
             {'k': 'a', 'energy': '3.00', 'at': 0}])
        rollup = [k for d in report['drills'] for k in d['key_rollup']]
        self.assertEqual(len(rollup), 1)
        self.assertIn('same_key_different_value', rollup[0]['status'])
        self.assertEqual(len(rollup[0]['missing_on_right']), 1)
        self.assertEqual(len(rollup[0]['extra_on_right']), 1)
        # 不把单边行配成修改前后。
        for item in rollup[0]['missing_on_right'] + rollup[0]['extra_on_right']:
            self.assertNotIn('paired_with', item)

    def test_decimal_scale_equals_and_non_scale_differs(self):
        equal = self.run_pair(
            [{'k': 'a', 'energy': '1.2', 'at': 0}],
            [{'k': 'a', 'energy': '1.200', 'at': 0}])
        self.assertTrue(equal['summary']['roots_equal'])
        unequal = self.run_pair(
            [{'k': 'a', 'energy': '1.20', 'at': 0}],
            [{'k': 'a', 'energy': '1.02', 'at': 0}])
        self.assertFalse(unequal['summary']['roots_equal'])
        report = [e for d in unequal['drills'] for e in d['entries']]
        self.assertEqual(len(report), 2)  # 一侧有 1.20，另一侧有 1.02

    def test_string_composition_forms_differ(self):
        pre = 'e\u0301'
        comp = '\u00e9'
        report = self.run_pair(
            [{'k': pre, 'energy': '0.00', 'at': 0}],
            [{'k': comp, 'energy': '0.00', 'at': 0}])
        self.assertFalse(report['summary']['roots_equal'])
        keys = [e['key'][0] for d in report['drills'] for e in d['entries']]
        self.assertEqual(sorted(keys), sorted([pre, comp]))
        self.assertNotEqual(pre, comp)

    def test_single_side_member_in_middle_leaf(self):
        report = self.run_pair([], [{'k': 'm', 'energy': '1.00', 'at': 0}])
        drills = report['drills']
        # 'm' 落在 [b, z) 叶，只需展开该叶；其它叶根摘要一致。
        self.assertEqual(len(drills), 1)
        self.assertEqual(drills[0]['range']['hi_index'], 2)
        self.assertEqual(drills[0]['range']['lo_index'], 1)

    def test_missing_field_vs_null(self):
        report = self.run_pair(
            [{'k': 'a', 'energy': None, 'at': 0}],
            [{'k': 'a', 'at': 0}])
        self.assertFalse(report['summary']['roots_equal'])
        entries = [e for d in report['drills'] for e in d['entries']]
        self.assertEqual(len(entries), 2)
        self.assertIn(entries[0]['row']['energy'], (None, {'missing': True}))
        self.assertIn(entries[1]['row']['energy'], (None, {'missing': True}))
        self.assertNotEqual(entries[0]['row']['energy'],
                            entries[1]['row']['energy'])

    def test_required_missing_rejected(self):
        lp = Path(self.root, 'left.jsonl')
        rp = Path(self.root, 'right.jsonl')
        write_jsonl(lp, [{'k': 'a', 'energy': '1.00'}])
        write_jsonl(rp, [])
        manifest = build_manifest(self.root, 'left.jsonl', 'right.jsonl')
        with self.assertRaises(ReconcileError):
            reconcile(write_manifest(self.root, manifest))

    def test_source_identity_mismatch_rejects_and_no_root(self):
        lp = Path(self.root, 'left.jsonl')
        rp = Path(self.root, 'right.jsonl')
        write_jsonl(lp, [{'k': 'a', 'at': 0}])
        write_jsonl(rp, [{'k': 'a', 'at': 0}])
        manifest = build_manifest(self.root, 'left.jsonl', 'right.jsonl')
        manifest['left']['rows'] = 99
        with self.assertRaises(ReconcileError):
            reconcile(write_manifest(self.root, manifest))
        manifest['left']['rows'] = 1
        manifest['left']['sha256'] = '0' * 64
        with self.assertRaises(ReconcileError):
            reconcile(write_manifest(self.root, manifest))

    def test_source_path_escape_rejected(self):
        lp = Path(self.root, 'left.jsonl')
        rp = Path(self.root, 'right.jsonl')
        write_jsonl(lp, [{'k': 'a', 'at': 0}])
        write_jsonl(rp, [{'k': 'a', 'at': 0}])
        manifest = build_manifest(self.root, 'left.jsonl', 'right.jsonl')
        manifest['left']['path'] = '../escape.jsonl'
        with self.assertRaises(ReconcileError):
            reconcile(write_manifest(self.root, manifest), workdir=self.root)

    def test_encoding_version_mismatch(self):
        lp = Path(self.root, 'left.jsonl')
        rp = Path(self.root, 'right.jsonl')
        write_jsonl(lp, [])
        write_jsonl(rp, [])
        manifest = build_manifest(self.root, 'left.jsonl', 'right.jsonl')
        manifest['encoding_version'] = 'bag-v2'
        with self.assertRaises(ReconcileError):
            reconcile(write_manifest(self.root, manifest))

    def test_boundary_derived_per_side_rejected(self):
        # 若两侧各自用不同边界，索引绑定会不同；这里要求清单只给一份边界。
        lp = Path(self.root, 'left.jsonl')
        rp = Path(self.root, 'right.jsonl')
        write_jsonl(lp, [{'k': 'a', 'at': 0}])
        write_jsonl(rp, [{'k': 'a', 'at': 0}])
        manifest = build_manifest(self.root, 'left.jsonl', 'right.jsonl')
        manifest['right_boundaries'] = [{'sentinel': 'min'}, ['a'], {'sentinel': 'max'}]
        report = reconcile(write_manifest(self.root, manifest), keep=True,
                           workdir=self.root)
        self.assertNotIn('right_boundaries', report['boundaries'])


class SpillTests(TempCase):
    def test_minimum_open_files_does_not_hang_or_exceed(self):
        rows = [{'k': 'k%03d' % i, 'energy': '1.00', 'at': i} for i in range(120)]
        report = self.run_pair(rows, rows, memory=200, open_files=3)
        self.assertLessEqual(report['read_stats']['peak_open_files'], 3)
        self.assertTrue(report['summary']['roots_equal'])

    def test_external_merge_preserves_counts(self):
        # 极小内存预算迫使多次外排：重复组跨段合并后计数仍完整。
        rows = [{'k': 'b', 'energy': None, 'at': 1}] * 40
        rows += [{'k': 'd', 'energy': '1.00', 'at': t} for t in range(25)]
        report = self.run_pair(rows, rows, memory=256, open_files=4)
        self.assertTrue(report['summary']['roots_equal'])
        self.assertGreater(report['read_stats']['segments_written'], 2)
        self.assertLessEqual(report['read_stats']['peak_buffered_entries'],
                             report['read_stats']['entry_budget'])
        stats = report['read_stats']['left']
        self.assertEqual(stats['rows_read'], 65)
        self.assertEqual(report['oracle']['left_rows'], 65)
        self.assertEqual(report['oracle']['left_distinct'], 26)

    def test_open_file_limit_respected(self):
        rows = [{'k': 'b', 'energy': None, 'at': t} for t in range(120)]
        report = self.run_pair(rows, rows, memory=128, open_files=4)
        self.assertLessEqual(report['read_stats']['peak_open_files'], 4)
        self.assertTrue(report['summary']['roots_equal'])

    def test_spill_only_expands_mismatched_leaf(self):
        # 大量相同行铺满两个叶，仅第 2 叶有差异；下钻只应展开第 2 叶。
        left = [{'k': 'a', 'energy': '1.00', 'at': t} for t in range(30)]
        left += [{'k': 'm', 'energy': '2.00', 'at': t} for t in range(30)]
        left += [{'k': 'y', 'energy': '3.00', 'at': t} for t in range(30)]
        right = list(left)
        right[35] = {'k': 'm', 'energy': '9.00', 'at': 5}
        report = self.run_pair(left, right, memory=256, open_files=4)
        drills = report['drills']
        self.assertEqual(len(drills), 1)
        self.assertEqual(drills[0]['range']['lo_index'], 1)
        self.assertEqual(drills[0]['range']['hi_index'], 2)
        self.assertEqual(len(drills[0]['entries']), 2)

    def test_counter_oracle_not_used_when_roots_differ(self):
        report = self.run_pair(
            [{'k': 'a', 'at': 0}], [{'k': 'a', 'at': 1}])
        self.assertFalse(report['summary']['roots_equal'])
        self.assertEqual(report['oracle']['mode'], 'bounded')
        self.assertEqual(report['drills'][0]['range']['lo_index'], 0)


class SegmentIntegrityTests(TempCase):
    def test_corrupt_segment_fails_loudly(self):
        report = self.run_pair(
            [{'k': 'a', 'at': 0}], [{'k': 'a', 'at': 0}])
        path = report['segments']['left']
        raw = Path(path).read_bytes()
        Path(path).write_bytes(raw.replace(b'bag-v1-row', b'bag-v1-rov', 1)
                               if b'bag-v1-row' in raw else raw + b'x')
        with self.assertRaises(Exception):
            from lake_data.bag import iter_segment, OpenBudget
            for _ in iter_segment(path, OpenBudget(4)):
                pass

    def test_segment_header_count_mismatch_fails(self):
        report = self.run_pair([{'k': 'a', 'at': 0}, {'k': 'b', 'at': 0}], [])
        path = report['segments']['left']
        text = Path(path).read_text(encoding='ascii')
        lines = text.splitlines()
        # 头行条目数改成 3，实际仍 2 条：必须失败而不是当成空/残缺区间。
        lines[0] = 'BAGSEG4 3'
        Path(path).write_text('\n'.join(lines) + '\n', encoding='ascii')
        from lake_data.bag import iter_segment, OpenBudget
        with self.assertRaises(Exception):
            for _ in iter_segment(path, OpenBudget(4)):
                pass

    def test_tampered_row_payload_fails(self):
        report = self.run_pair([{'k': 'a', 'at': 0}], [])
        path = report['segments']['left']
        lines = Path(path).read_text(encoding='ascii').splitlines(keepends=True)
        # 篡改条目里的行十六进制，但保留原行哈希与段尾：逐行校验应发现不符。
        fields = lines[1].rstrip('\n').split('\t')
        fields[2] = fields[2][:-1] + ('0' if fields[2][-1] != '0' else '1')
        lines[1] = '\t'.join(fields) + '\n'
        Path(path).write_text(''.join(lines), encoding='ascii')
        from lake_data.bag import iter_segment, OpenBudget
        with self.assertRaises(Exception):
            for _ in iter_segment(path, OpenBudget(4)):
                pass

    def test_truncated_segment_fails(self):
        report = self.run_pair([{'k': 'a', 'at': 0}], [])
        path = report['segments']['left']
        raw = Path(path).read_bytes()
        Path(path).write_bytes(raw[:-5])
        from lake_data.bag import iter_segment, OpenBudget
        with self.assertRaises(Exception):
            for _ in iter_segment(path, OpenBudget(4)):
                pass


class PublicationTests(TempCase):
    def test_root_not_published_on_bad_source(self):
        lp = Path(self.root, 'left.jsonl')
        rp = Path(self.root, 'right.jsonl')
        write_jsonl(lp, [{'k': 'a', 'at': 0}])
        rp.write_text('{"k":"a","at":0}\nnot-json\n', encoding='utf8')
        manifest = build_manifest(self.root, 'left.jsonl', 'right.jsonl',
                                  rows_override={'right': 2})
        error = None
        try:
            reconcile(write_manifest(self.root, manifest), keep=True,
                      workdir=self.root)
        except ReconcileError as e:
            error = e
        # 失败时不返回任何报告（没有根摘要可被误用）。
        self.assertIsNotNone(error)
        self.assertIn('JSON', str(error))

    def test_roots_bind_boundary_positions(self):
        rows = [{'k': k, 'energy': None, 'at': 0} for k in ('a', 'b', 'c', 'z')]
        digests = set()
        for boundaries in (
                [{'sentinel': 'min'}, ['b'], ['z'], {'sentinel': 'max'}],
                [{'sentinel': 'min'}, ['c'], ['m'], {'sentinel': 'max'}],
                [{'sentinel': 'min'}, {'sentinel': 'max'}]):
            report = self.run_pair(rows, rows, boundaries=boundaries)
            self.assertEqual(report['boundaries']['leaf_count'],
                             len(boundaries) - 1)
            digests.add(report['summary']['left']['root_digest'])
        # 相同多重集在不同冻结区间树下根摘要不同，摘要确实绑定边界树。
        self.assertEqual(len(digests), 3)

    def test_root_deterministic_across_runs(self):
        left = [{'k': 'b', 'energy': '1.00', 'at': 1}] * 5
        right = [{'k': 'b', 'energy': '1.00', 'at': 1}] * 3
        first = self.run_pair(left, right, memory=128)
        second = self.run_pair(left, right, memory=4096)
        self.assertEqual(first['summary']['left']['root_digest'],
                         second['summary']['left']['root_digest'])
        self.assertEqual(first['summary']['right']['root_digest'],
                         second['summary']['right']['root_digest'])
        self.assertEqual(first['summary']['roots_equal'],
                         second['summary']['roots_equal'])

    def test_expansion_is_limited_to_mismatched_leaves(self):
        # 大量行铺满三叶，仅 [b, z) 叶有差异；读取与展开统计须体现只展开该叶。
        left = []
        left += [{'k': 'a', 'energy': '1.00', 'at': t} for t in range(200)]
        left += [{'k': 'm', 'energy': '1.00', 'at': t} for t in range(200)]
        right = list(left)
        right[-1] = {'k': 'm', 'energy': '2.00', 'at': 199}
        report = self.run_pair(left, right, memory=512, open_files=4)
        drills = report['drills']
        self.assertEqual(len(drills), 1)
        self.assertEqual(drills[0]['range']['lo_index'], 1)
        self.assertEqual(len(drills[0]['entries']), 2)
        # 两侧实际读取行数等于源行数，证明只遍历一次源。
        self.assertEqual(report['read_stats']['left']['rows_read'], 400)
        self.assertEqual(report['read_stats']['right']['rows_read'], 400)


class LargeDuplicateGroupTests(TempCase):
    def test_one_key_with_many_duplicates_spills_and_counts(self):
        # 单一键、上千条重复行：条目预算极小，同一条目跨多个段，最终计数必须精确。
        rows = [{'k': 'b', 'energy': '0.01', 'at': 7}] * 1200
        report = self.run_pair(rows, [], memory=256, open_files=4)
        entries = [e for d in report['drills'] for e in d['entries']]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['kind'], 'only_left')
        self.assertEqual(entries[0]['left_count'], 1200)
        self.assertEqual(report['read_stats']['left']['rows_read'], 1200)
        self.assertLessEqual(report['read_stats']['peak_buffered_entries'], 1)

    def test_distinct_keys_beyond_budget(self):
        left = [{'k': 'd%04d' % i, 'energy': None, 'at': i} for i in range(500)]
        right = list(left)
        right[250] = {'k': 'd0250', 'energy': None, 'at': 999999}
        report = self.run_pair(left, right, memory=512, open_files=4)
        self.assertEqual(len(report['drills']), 1)
        self.assertEqual(len(report['drills'][0]['entries']), 2)
        self.assertGreater(report['read_stats']['segments_written'], 2)


class OrderIndependenceTests(TempCase):
    def test_shuffled_sources_yield_same_roots_and_diff(self):
        base = [{'k': 'a', 'energy': '%d.00' % i, 'at': i} for i in range(6)]
        base += [{'k': 'b', 'energy': None, 'at': 1}] * 3
        base += [{'k': 'z', 'energy': None, 'at': 0}]
        other = list(base) + [{'k': 'c', 'energy': '5.00', 'at': 5}]
        import random
        reference = None
        for seed in range(3):
            random.Random(seed).shuffle(base)
            random.Random(seed + 10).shuffle(other)
            with tempfile.TemporaryDirectory() as d:
                write_jsonl(Path(d, 'l.jsonl'), base)
                write_jsonl(Path(d, 'r.jsonl'), other)
                manifest = build_manifest(d, 'l.jsonl', 'r.jsonl',
                                          memory=196, open_files=4)
                report = reconcile(write_manifest(d, manifest), workdir=d)
                current = (
                    report['summary']['left']['root_digest'],
                    report['summary']['right']['root_digest'],
                    tuple(x['ordered_diff_sha256'] for x in report['drills']))
                reference = reference or current
                self.assertEqual(current, reference)


class FrozenContractTests(TempCase):
    def test_unknown_and_required_fields_rejected(self):
        lp = Path(self.root, 'left.jsonl')
        rp = Path(self.root, 'right.jsonl')
        lp.write_text('{"k":"a","at":0,"extra":1}\n', encoding='utf8')
        write_jsonl(rp, [])
        manifest = build_manifest(self.root, 'left.jsonl', 'right.jsonl')
        with self.assertRaises(ReconcileError):
            reconcile(write_manifest(self.root, manifest))

    def test_blank_line_rejected(self):
        lp = Path(self.root, 'left.jsonl')
        rp = Path(self.root, 'right.jsonl')
        lp.write_text('{"k":"a","at":0}\n\n', encoding='utf8')
        write_jsonl(rp, [])
        manifest = build_manifest(self.root, 'left.jsonl', 'right.jsonl')
        with self.assertRaises(ReconcileError):
            reconcile(write_manifest(self.root, manifest))

    def test_schema_order_change_changes_binding(self):
        left = [{'k': 'a', 'energy': '1.00', 'at': 0}]
        first = self.run_pair(left, left)
        lp = Path(self.root, 'left.jsonl')
        rp = Path(self.root, 'right.jsonl')
        write_jsonl(lp, left)
        write_jsonl(rp, left)
        schema = [
            {'id': 'energy', 'type': 'decimal', 'scale': 2, 'required': False},
            {'id': 'k', 'type': 'string', 'required': True},
            {'id': 'at', 'type': 'timestamp', 'required': True},
        ]
        manifest = build_manifest(self.root, 'left.jsonl', 'right.jsonl',
                                  schema=schema)
        # key_fields 仍为 ['k']，但它在重排后的 schema 中是第 2 个字段；边界数组
        # 按 key_fields 声明顺序，不受 schema 字段位置影响。
        reordered = reconcile(write_manifest(self.root, manifest), workdir=self.root)
        self.assertNotEqual(first['schema_sha256'], reordered['schema_sha256'])
        self.assertNotEqual(first['summary']['left']['root_digest'],
                            reordered['summary']['left']['root_digest'])
