"""eval · tally 评测路（问题集 + 四项指标测量 + 同问两遍字节比对 + 打回清单）。

只碰 eval/：本包不读 ask/ show/ dict/ research/ 的源码，也不改它们。
测量对象是「answer.json 产物」（契约 contract/answer.md §2），通过路径参数喂进来。
"""
