# 规范编码与范围树

编码采用固定字段顺序，行先写字段数u32大端，字段ID为UTF8长度前缀；每值有类型字节和u64大端payload长度。null单字节类型N、payload长度0；string S为原UTF8，bytes B输入base64解码，long L为i64大端，boolean T为00/01，timestamp U为UTC微秒i64，decimal D为声明scale的i64整数并附i32 scale，超i64失败。缺可选字段用独立M编码，不规范成null。类型标记和长度避免组合碰撞。

排序比较按schema类型值，不按长度前缀序；null先于缺失再非空，string按Unicode，bytes按无符号字节，decimal按精确整数，timestamp按整数。完整行相等是编码相等；业务键相同可含不同全行。边界为key_fields元组的规范类型值，min/max是独立哨兵，不与字符串同名混同。两侧使用同一棵按叶顺序两两合并的二叉树，奇数层末节点与显式empty-child合并，不复制末叶。

叶摘要为domain-tag、边界编码、按完整规范编码排序的(row SHA256,count u64)流、总行数与不同编码数的SHA256；若SHA碰撞对应不同编码，仍按完整编码分组并报告碰撞不能报一致。父摘要含domain-tag、完整覆盖区间、左/右摘要。损坏段有长度与SHA256，Counter小基准只用于核对，不在大流正式算法里积累全部键。源只可遍历一次，差异叶原行从封存外排段读取，不重新遍历原流。
