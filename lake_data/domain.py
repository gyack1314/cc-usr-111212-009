"""核对对账流文件和类型清单，不生成袋摘要。"""
from .io import asset,unique,integer

def summarize(v,root):
    unique(v['schema'],'id');integer(v['memory_bytes'],1);integer(v['max_open_files'],3)
    left,_=asset(root,v['left']);right,_=asset(root,v['right'])
    return {'left_raw_bytes':len(left),'right_raw_bytes':len(right),'fields':len(v['schema']),'frozen_intervals':len(v['boundaries'])-1}
