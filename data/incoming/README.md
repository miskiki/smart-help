# 全国政策数据接入目录（data/incoming）

把待接入的全国政策数据文件放在本目录，支持 .csv / .xlsx / .json。
默认 dry-run 只读检查、不写库：

    cd <repo> && set -a && . deploy/web.env && set +a
    PYTHONPATH=src:. python deploy/national_intake.py            # 扫描本目录，只读报告
    PYTHONPATH=src:. python deploy/national_intake.py --json     # 机器可读报告
    PYTHONPATH=src:. python deploy/national_intake.py --offline  # 完全不连库
    PYTHONPATH=src:. python deploy/national_intake.py --apply    # 写库（需 POLICY_IMPORT=1）

字段约定、编码、批次与回滚说明见 docs/redesign/22-national-intake.md。
