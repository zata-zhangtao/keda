# Alembic Migration Standards

本页定义本仓库 Alembic 迁移脚本的命名与生成约束。

## File Name Format

新生成的迁移脚本必须使用：

```text
YYYYMMDD_HHMMSS_<slug>.py
```

- 时间戳取运行命令时本地 `date +%Y%m%d_%H%M%S`，必须精确到秒。
- `slug` 使用小写蛇形命名，并描述迁移目的。
- `revision` 必须等于文件名去掉 `.py` 后的时间戳前缀。
- `down_revision` 必须指向创建时 `alembic heads` 的唯一 head。

## Required Generation Entry Point

禁止手工创建、重命名或编造迁移时间戳。必须从仓库根目录执行：

```bash
just new-migration <slug>
```

该入口调用 `scripts/shared/alembic/new_migration.sh`，会验证版本图只有一个 head、调用 Alembic 模板生成文件，并同步文件名、`revision` 与 `down_revision`。只在生成后用补丁填写 `upgrade()` 和 `downgrade()`。

交付前执行：

```bash
uv run alembic heads
```

输出必须只有一个 head，且应为新迁移的 revision。

## Backfill Idempotency

回填型迁移（upgrade 中 UPDATE/DELETE 现有数据）必须容忍重复执行。`env.py` 的 `transaction_per_migration` 已防住 MySQL DDL 隐式提交导致的 version 滞后，但 `alembic_version` 仍可能因手动 `alembic stamp` 回旧版本、备份部分恢复或 downgrade 中断而落后于实际 schema；此时重跑迁移是正常路径，回填逻辑必须安全。

### 唯一约束下的回填陷阱

单条 `UPDATE` 在有唯一键时会逐行检查约束，目标值与未更新行的当前值在中间状态构成置换就会撞键：例如某 session 当前 `sequence = [1, 0, 2]`，`ROW_NUMBER()` 目标 = `[1, 2, 3]`，把第二行 `0 -> 2` 时第三行仍是 `2`，立即冲突。MySQL 逐行立即检查；PostgreSQL 默认 IMMEDIATE 唯一约束同样逐行检查（实测 PostgreSQL 17 亦报 `UniqueViolation`）。这是方言级陷阱，看 SQL 字面逻辑无法发现。

### 推荐写法

涉及唯一约束/唯一索引列的回填，用"先移除约束 -> 回填 -> 重建约束"模式，让回填不受约束限制；DDL 后刷新 inspector 再判断是否重建：

```python
if "uq_xxx" in existing_constraint_names:
    op.drop_constraint("uq_xxx", "table_name", type_="unique")
    inspector = sa.inspect(op.get_bind())  # DDL 后刷新，确保后续 create 判断反映最新 schema
op.execute(sa.text("UPDATE ... SET col = ROW_NUMBER() ..."))
if "uq_xxx" not in refreshed_constraint_names:
    op.create_unique_constraint("uq_xxx", "table_name", [...])
```

不涉及唯一约束的回填，也要保证重跑幂等：赋值在重跑时不变，或用 `WHERE col = <default>` 只处理未回填的行。downgrade 同样要可重跑。
