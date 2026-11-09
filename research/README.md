# 公开方案检索与条件提交：2026-11-06

## 门槛与结果

- 开始与提交前实时核对的账号最高分：`25.08686`，提交 `58041165`。
- 本次本地估分：`25.086887305261655`。
- 用现有线上分数及化学成本比例配对推算：`25.08688203472144`。
- 考虑现有分数五位小数舍入的保守配对推算：`25.086877034717048`。
- 改进幅度很小，预计榜单约提高 `0.00002`。本地估分不等于线上分数。
- `receipts/submission-gate.json` 记录提交瞬间的实时比较；只有两种估计都超过实时最高分才允许上传。
- 线上创建与最终评分状态分别存于 `receipts/submission-receipt.json`、`receipts/submission-status.json`。
- 已成功创建线上提交 `58044160`；是否已评分以状态文件为准。

## 公开来源与版本审计

检索 Discussion 的最新、热门、最高票列表，并获取相关正文；同时查询公开 notebook 的 scoreDescending 列表，快照见 `sources/top-public-notebooks.csv`。

- 最新 Discussion 汇总：https://www.kaggle.com/competitions/google-verifiable-quantum-circuit-compilation/discussion/756210
- 公开榜首来源：https://www.kaggle.com/code/heliosli/the-dataset-is-public-now-enjoy
- 下载的数据集：https://www.kaggle.com/datasets/heliosli/qcc-circuits-live
- 发布清单：`sources/heliosli-release/QCC_RELEASE_MANIFEST.json`，portfolio `v76_sy5_ay3`。全部文件已逐一核对 SHA-256。
- Syed Asad Ali 的上游：https://www.kaggle.com/code/syed3000/vqcc-own-circuit-portfolio-notebook
- 保留的原始署名与许可：`sources/heliosli-release/CREDITS.md`、`LICENSE.md`，以及对应 notebook 的来源说明。

公开汇总的数据更新慢于实时 notebook。最终以实际下载的最新发布清单为准。用户现有逆平方根比这个公开方案更便宜，所以没有用较弱的公开文件覆盖它。公开方案的 DQI 和 15 个化学加载电路，与用户现有最优文件逐字节一致；数值权重略有改进。

## 本次自有改动

1. `exact_rewrite.py` 对可交换的自逆门作精确抵消。化学的 15 个加载器合计消除 59,885 对 CX，其他计分门、寄存器数量不变。优化对任意基态与条件位成立；没有以未知初始状态假定辅助位为零。
2. `rotate_weights.py` 对各组的权重列与 H2_I 同步作正交 Givens 旋转。使用分段解析目标寻找列对最优旋转，连续 60 轮。保持 W W^T、W h^T 与 ||h||²，在精确算术下保持重构张量、单体算符和 SOS gap；仅降低归一化成本。
3. 逆平方根与 DQI 保留用户当前最优提交的原始字节。没有通过改写哈希选择更有利的测试抽样。

化学成本从用户现有的 `299673103.4280183` 降为 `299672313.7873349`。lambda_eff 从 `46.893662078700515` 降为 `46.89353851357925`。Toffoli 14966、量子比特 427 不变。

纯 CX 消除不直接提升比赛成本；它减少电路指令量。分数收益来自公开权重小幅改进与本次正交旋转。逆平方根上也测试了门抵消，但计分门不变，最终没有采纳，以保持配对比较稳定。

## 验证证据

- 原始最优逆平方根：完整 200,540,928 shots，PASS。
- 保留的 DQI：完整 102,400 shots，0 输出错误、0 相位错误，PASS。
- 最终化学：两个独立命名 salt，各完整 78,080 shots，PASS，成本完全相同。
- 官方 metric 的门集、寄存器、数组类型、形状和大小预检均通过。
- 正交变换后不变量最大误差约 `8.4e-16`、`1.4e-15`、`7.1e-15`。
- `receipts/identity-checks.json`：40 个随机实例的解析旋转最小值与密集网格比较；50 个随机 5 量子比特电路逐一穷举基态和条件位，比较输出与相位，共 6,400 次比较，通过。
- 验证器未修改。线上秘密 salt 不可复现，最终成绩须看线上回执。

## 复现入口

从项目根目录运行。完整中间记录位于 `receipts/`，候选位于 `candidates/`。

```bash
.venv/bin/python research/exact_rewrite.py INPUT.kmb OUTPUT.kmb
OPENBLAS_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 .venv/bin/python research/rotate_weights.py \
  research/candidates/public-best/dfthcsos/submission.npz \
  research/candidates/rotated50/dfthcsos/submission.npz --passes 60

# 已有提交回执时脚本会拒绝重复上传
.venv/bin/python research/submit_improvement.py
.venv/bin/python scripts/kaggle_submission.py status \
  --receipt research/receipts/submission-receipt.json
```

目录名 rotated50 是最初实验名；最终实际运行 60 轮，以 rotation.json 与日志为准。认证使用临时目录或 Kaggle 标准配置，Token 不在项目中。
