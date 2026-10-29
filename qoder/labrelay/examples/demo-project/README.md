# Demo Research Project / 合成示例科研资料

该目录是一个**完全合成**的微型科研项目，用来让评委和开发者在本地复现 LabRelay 的主要流程：
导入资料 → 混合检索 → 学长交接 → 新人测评 → 生成七阶段学习路径 → 推进与重规划 → 反向交接。

项目名称 Aurora 时序异常检测、论文、代码、指标、服务器规则均为虚构，不对应任何真实实验室。

## 目录结构

```text
examples/demo-project/
├── papers/     合成论文节选（PDF，可抽取正文）
├── code/       train.py / evaluate.py 两个入口的最小骨架
├── configs/    baseline.yaml —— 产出历史指标的唯一配置
├── experiments/metrics_baseline.md —— 历史指标、验收线、已知失败
├── notes/      交接说明、复现记录、凭据检测样例
└── ops/        服务器与资源申请/释放规则
```

## 六个验证问题与答案出处

导入本目录后，下面六个问题都应能检索到带引用的答案：

| 验证问题               | 答案所在资料                        |
| ---------------------- | ----------------------------------- |
| 如何复现 baseline      | `notes/reproduction_log.md`         |
| 实验配置在哪里         | `configs/baseline.yaml`             |
| 历史指标是多少         | `experiments/metrics_baseline.md`   |
| 已知数据缓存问题是什么 | `notes/handoff.md`                  |
| 下一项未完成工作是什么 | `notes/handoff.md` 与论文节选结论页 |
| 实验室资源如何申请释放 | `ops/lab_server_resources.md`       |

七个阶段的必需证据类型（operations / note / paper / code / config / experiment）在本目录里各有
至少一份对应资料，因此用合成模型网关也能生成出可激活的完整学习路径。

## 内置的安全样例

`notes/credential_scan_demo.md` 里含有一条**明显不可用的占位凭据**，用来演示导入阶段的凭据检测：
该文件会被标为 restricted，只保存不索引，检索与引用都不会命中它。导入结果里的
`restricted` 计数应当包含它，这是预期行为。

## 禁止放入

- 真实实验室服务器地址、账号或内网信息；
- API Key、数据库密码、私钥或其他凭据；
- 未公开或不可再分发的论文全文；
- 原始大型数据集和模型权重；
- 姓名、联系方式等个人身份信息。
