"""规范编码、类型序与冻结边界的边界用例。"""
import unittest

from lake_data.codec import (Schema, BoundaryTree, canon_decimal, canon_timestamp,
                             value_bytes, value_ord, key_ord, type_value_cmp,
                             dequote, NULL_BYTES, MISSING_BYTES)

SCHEMA = [
    {'id': 'k', 'type': 'string', 'required': True},
    {'id': 'energy', 'type': 'decimal', 'scale': 2, 'required': False},
    {'id': 'at', 'type': 'timestamp', 'required': True},
]


def sample_schema():
    return Schema(SCHEMA, ['k'])


class EncodingTests(unittest.TestCase):
    def test_schema_identity_binds_order_and_scale(self):
        s = sample_schema()
        reordered = Schema([SCHEMA[1], SCHEMA[0], SCHEMA[2]], ['k'])
        rescaled = Schema([SCHEMA[0], dict(SCHEMA[1], scale=3), SCHEMA[2]], ['k'])
        self.assertNotEqual(s.digest, reordered.digest)
        self.assertNotEqual(s.digest, rescaled.digest)
        self.assertEqual(s.digest, sample_schema().digest)

    def test_decimal_scale_equivalence_and_rejection(self):
        self.assertEqual(canon_decimal('1.20', 2), 120)
        self.assertEqual(canon_decimal('1.2', 2), 120)
        self.assertEqual(canon_decimal('0.00', 2), 0)
        self.assertEqual(canon_decimal('0', 2), 0)
        with self.assertRaises(ValueError):
            canon_decimal('1.234', 2)      # 隐式舍入不允许
        with self.assertRaises(ValueError):
            canon_decimal(1.2, 2)          # 二进制浮点不允许
        with self.assertRaises(ValueError):
            canon_decimal(True, 2)
        with self.assertRaises(ValueError):
            canon_decimal(str(1 << 62), 2)  # 定标后超 i64
        self.assertEqual(canon_decimal([120, 2], 2), 120)
        with self.assertRaises(ValueError):
            canon_decimal([120, 3], 2)

    def test_null_missing_and_empty_are_distinct(self):
        s = sample_schema()
        null_row = s.row_values({'k': 'a', 'energy': None, 'at': 0})
        missing_row = s.row_values({'k': 'a', 'at': 0})
        self.assertNotEqual(s.canonical(null_row)['row_sha256'],
                            s.canonical(missing_row)['row_sha256'])
        # null 与缺失的规范类型值本身也不同，可读报告能区分。
        self.assertEqual(dequote(null_row[1]), None)
        self.assertEqual(dequote(missing_row[1]), {'missing': True})

    def test_string_not_normalized(self):
        s = sample_schema()
        pre = 'é'
        comp = 'é'
        self.assertNotEqual(pre, comp)
        v_pre = s.row_values({'k': pre, 'energy': '0.00', 'at': 2})
        v_comp = s.row_values({'k': comp, 'energy': '0.00', 'at': 2})
        self.assertNotEqual(s.canonical(v_pre)['row_sha256'],
                            s.canonical(v_comp)['row_sha256'])

    def test_required_missing_rejected(self):
        s = sample_schema()
        with self.assertRaises(ValueError):
            s.row_values({'energy': '1.00', 'at': 0})
        with self.assertRaises(ValueError):
            s.row_values({'k': 'a'})

    def test_unknown_field_and_bad_types(self):
        s = sample_schema()
        with self.assertRaises(ValueError):
            s.row_values({'k': 'a', 'at': 0, 'extra': 1})
        with self.assertRaises(ValueError):
            s.row_values({'k': 1, 'at': 0})
        with self.assertRaises(ValueError):
            s.row_values({'k': 'a', 'at': 1.5})
        with self.assertRaises(ValueError):
            s.row_values({'k': 'a', 'at': True})

    def test_type_order_not_length_prefix(self):
        s = sample_schema()
        short = value_bytes(s.field_map['k'], 'a')
        longer = value_bytes(s.field_map['k'], 'bb')
        self.assertLess(value_ord(short), value_ord(longer))
        # 字符串 'z' 与 'aa'：按内容比较，不按长度前缀。
        self.assertGreater(value_ord(value_bytes(s.field_map['k'], 'z')),
                           value_ord(value_bytes(s.field_map['k'], 'aa')))
        self.assertLess(value_ord(NULL_BYTES), value_ord(MISSING_BYTES))
        self.assertLess(value_ord(MISSING_BYTES), value_ord(short))

    def test_value_ord_injective_and_prefix_free(self):
        s = sample_schema()
        values = ['', 'a', 'aa', 'a!', 'b', '0', '00']
        ords = [value_ord(value_bytes(s.field_map['k'], v)) for v in values]
        self.assertEqual(len(set(ords)), len(values))
        # 元组序由 '!' 收尾，某字段更长不会让更高位字段的值“占便宜”。
        self.assertLess(value_ord(value_bytes(s.field_map['k'], 'a')),
                        value_ord(value_bytes(s.field_map['k'], 'aa')))
        self.assertLess(value_ord(value_bytes(s.field_map['k'], 'a')),
                        value_ord(value_bytes(s.field_map['k'], 'b')))

    def test_ord_matches_typed_comparison(self):
        import itertools
        import random
        s = Schema([{'id': 'a', 'type': 'string', 'required': True},
                    {'id': 'b', 'type': 'long', 'required': True},
                    {'id': 'c', 'type': 'string', 'required': False}], ['a', 'b'])
        rows = []
        for x, y, z in itertools.product(
                ['', 'a', 'a!', 'b', '\u00e9'], [-2 ** 63, -1, 0, 1, 2 ** 63 - 1],
                [None, 'missing', 'a']):
            obj = {'a': x, 'b': y}
            if z != 'missing':
                obj['c'] = z
            rows.append(s.row_values(obj))

        def typed(v, w):
            for i in s.key_index:
                c = type_value_cmp(v[i], w[i])
                if c:
                    return c
            for x, y2 in zip(v, w):
                c = type_value_cmp(x, y2)
                if c:
                    return c
            return 0
        rng = random.Random(7)
        for _ in range(2000):
            v, w = rng.choice(rows), rng.choice(rows)
            left = s.canonical(v)['row_order']
            right = s.canonical(w)['row_order']
            self.assertEqual((left > right) - (left < right), typed(v, w))

    def test_negative_numbers_ordered(self):
        s = sample_schema()
        neg = value_bytes(s.field_map['energy'], '-1.00')
        pos = value_bytes(s.field_map['energy'], '2.00')
        self.assertLess(value_ord(neg), value_ord(pos))
        big = value_bytes(s.field_map['energy'], '99.00')
        self.assertGreater(value_ord(big), value_ord(pos))
        z = value_bytes(s.field_map['energy'], '0.00')
        self.assertLess(value_ord(neg), value_ord(z))
        self.assertLess(value_ord(z), value_ord(pos))

    def test_multi_key_ordering_tuple_wise(self):
        s = Schema([{'id': 'a', 'type': 'long', 'required': True},
                    {'id': 'b', 'type': 'string', 'required': True}], ['a', 'b'])
        v1 = s.row_values({'a': 1, 'b': 'z'})
        v2 = s.row_values({'a': 2, 'b': 'a'})
        self.assertLess(key_ord(s, v1), key_ord(s, v2))
        # 每个分量以 '!' 收尾，拼接后不会被后继字段的值干扰。
        self.assertEqual(key_ord(s, v1), '28000000000000001!27a!')
        self.assertEqual(key_ord(s, v2), '28000000000000002!261!')

    def test_timestamp_utc_only(self):
        self.assertEqual(canon_timestamp(0), 0)
        self.assertEqual(canon_timestamp('1970-01-01T00:00:00Z'), 0)
        self.assertEqual(canon_timestamp('1970-01-01T00:00:00.000001Z'), 1)
        with self.assertRaises(ValueError):
            canon_timestamp('1970-01-01T00:00:00+08:00')
        with self.assertRaises(ValueError):
            canon_timestamp('1970-02-30T00:00:00Z')
        with self.assertRaises(ValueError):
            canon_timestamp(True)

    def test_row_roundtrip_all_types(self):
        rows = [
            {'k': 'a', 'n': -3, 'b': 'YWI=', 't': False, 'u': -5, 'd': '-1.20'},
            {'k': 'z', 'n': 2 ** 63 - 1, 'b': '', 't': True,
             'u': "9999-12-31T23:59:59.999999Z", 'd': '0'},
            {'k': 'm', 'n': None, 'b': None, 't': None, 'u': None, 'd': None},
        ]
        schema = [
            {'id': 'k', 'type': 'string', 'required': True},
            {'id': 'n', 'type': 'long', 'required': False},
            {'id': 'b', 'type': 'bytes', 'required': False},
            {'id': 't', 'type': 'boolean', 'required': False},
            {'id': 'u', 'type': 'timestamp', 'required': False},
            {'id': 'd', 'type': 'decimal', 'scale': 2, 'required': False},
        ]
        s2 = Schema(schema, ['k'])
        for row in rows:
            values = s2.row_values(row)
            self.assertEqual(s2.parse_row(s2.row_bytes(values)), values)
        # 篡改字段 ID 会被拒绝，而不是解出另一行。
        raw = s2.row_bytes(s2.row_values(rows[0]))
        tampered = raw.replace(b'F' + (1).to_bytes(8, 'big') + b'k',
                               b'F' + (1).to_bytes(8, 'big') + b'x', 1)
        with self.assertRaises(ValueError):
            s2.parse_row(tampered)

    def test_dequote_reports_types(self):
        s = sample_schema()
        v = s.row_values({'k': 'a', 'energy': None, 'at': 0})
        self.assertEqual(dequote(v[0]), 'a')
        self.assertEqual(dequote(v[1]), None)
        self.assertEqual(dequote(v[2]), {'utc_micros': 0})


class BoundaryTests(unittest.TestCase):
    def test_shared_tree_and_intervals(self):
        s = sample_schema()
        t = BoundaryTree(s, [{'sentinel': 'min'}, ['b'], ['z'], {'sentinel': 'max'}])
        self.assertEqual(t.n_leaves, 3)
        self.assertEqual(t.interval_text(0, 1), 'b[min|k1:262!]')
        self.assertEqual(t.interval_text(2, 3), 'b[k2:27a!|max]')
        # 左闭右开：'b' 属第 2 叶，'z' 属第 3 叶。
        self.assertEqual(t.leaf_index(s.row_values({'k': 'b', 'at': 0}), ), 1)
        self.assertEqual(t.leaf_index(s.row_values({'k': 'z', 'at': 0}), ), 2)
        self.assertEqual(t.leaf_index(s.row_values({'k': 'zz', 'at': 0}), ), 2)
        self.assertEqual(t.leaf_index(s.row_values({'k': 'a', 'at': 0}), ), 0)

    def test_boundary_ord_independent_of_schema_field_position(self):
        # 同一边界数组在字段重排后应得到相同的键序与树摘要。
        base = [{'id': 'k', 'type': 'string', 'required': True},
                {'id': 'v', 'type': 'long', 'required': True}]
        moved = [base[1], base[0]]
        boundaries = [{'sentinel': 'min'}, ['b', 1], ['z', 2], {'sentinel': 'max'}]
        t1 = BoundaryTree(Schema(base, ['k', 'v']), boundaries)
        t2 = BoundaryTree(Schema(moved, ['k', 'v']), boundaries)
        # 键序按 key_fields 声明顺序，与字段位置无关；树摘要另行绑定 schema 摘要，
        # 因此 schema 重排会改变树摘要，这是有意为之。
        self.assertEqual(t1.internal_ord, t2.internal_ord)
        self.assertNotEqual(t1.digest, t2.digest)

    def test_boundary_must_increase(self):
        s = sample_schema()
        with self.assertRaises(ValueError):
            BoundaryTree(s, [{'sentinel': 'min'}, ['b'], ['a'], {'sentinel': 'max'}])
        with self.assertRaises(ValueError):
            BoundaryTree(s, [{'sentinel': 'min'}, ['b'], ['b'], {'sentinel': 'max'}])
        with self.assertRaises(ValueError):
            BoundaryTree(s, [['b'], {'sentinel': 'max'}])
        # 只有 min/max 是合法单叶树，覆盖整个键域。
        whole = BoundaryTree(s, [{'sentinel': 'min'}, {'sentinel': 'max'}])
        self.assertEqual(whole.n_leaves, 1)
        with self.assertRaises(ValueError):
            BoundaryTree(s, [{'sentinel': 'min'}, ['b', 'x'], {'sentinel': 'max'}])
        with self.assertRaises(ValueError):
            # 内部哨兵直接拒绝，不把哨兵和类型值混为一谈。
            BoundaryTree(s, [{'sentinel': 'min'}, {'sentinel': 'min'},
                             {'sentinel': 'max'}])

    def test_sentinel_not_equal_to_string(self):
        s = sample_schema()
        t1 = BoundaryTree(s, [{'sentinel': 'min'}, ['min'], {'sentinel': 'max'}])
        t2 = BoundaryTree(s, [{'sentinel': 'min'}, ['max'], {'sentinel': 'max'}])
        self.assertNotEqual(t1.digest, t2.digest)
        self.assertNotEqual(sample_schema().digest,
                            BoundaryTree(s, t1.raw).digest)

    def test_odd_tail_uses_empty_child(self):
        s = sample_schema()
        t = BoundaryTree(s, [{'sentinel': 'min'}, ['b'], ['z'],
                             {'sentinel': 'max'}])
        self.assertEqual(t.n_leaves, 3)
        # 3 叶两两合并：第 2 个父节点是末叶与显式 empty-child，不复制末叶。
        empty = t.structure['empty_slots']
        self.assertEqual(len(empty), 1)
        self.assertEqual((empty[0]['lo'], empty[0]['hi']), (3, 3))
        self.assertEqual(t.structure['root']['hi'], 3)


if __name__ == '__main__':
    unittest.main()
