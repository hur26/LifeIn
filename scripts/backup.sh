#!/usr/bin/env bash
#
# 备份一次(P4 第 7 片)。
#
# 03 的 P4 硬门槛第 3 条是"**备份与恢复演练成功至少一次**" ——
# 注意它要的不是"配了备份",是"**恢复演练过**"。这个脚本只做前半件,
# 后半件在 `scripts/restore-drill.md` 里,而**只做前半件不算过门槛**。
#
# 一份从来没有被还原过的备份,和没有备份的区别只有一个:
# 前者让你以为自己有备份。
#
# 用法:
#     scripts/backup.sh                     # 用默认目录
#     BACKUP_DIR=/mnt/nas scripts/backup.sh
#
# 环境变量:
#     PGDATABASE   库名,默认 lifein
#     BACKUP_DIR   放哪,默认 /var/backups/lifein
#     KEEP_DAYS    留几天,默认 14

set -euo pipefail

DB="${PGDATABASE:-lifein}"
DIR="${BACKUP_DIR:-/var/backups/lifein}"
KEEP="${KEEP_DAYS:-14}"
STAMP="$(date +%F-%H%M)"
OUT="$DIR/lifein-$STAMP.dump"

mkdir -p "$DIR"

# --format=custom:能选择性还原单表,而灾难恢复之外的场景多半只想要一张表
pg_dump -d "$DB" --format=custom --file="$OUT"

# 立刻验一次能不能读。**这一步不能省** —— pg_dump 成功不等于文件是完整的,
# 而发现它坏了的时刻通常是你最需要它的那一刻
if ! pg_restore --list "$OUT" > /dev/null 2>&1; then
    echo "备份写出来了但读不回去:$OUT" >&2
    exit 1
fi

SIZE="$(du -h "$OUT" | cut -f1)"
COUNT="$(pg_restore --list "$OUT" | grep -c 'TABLE DATA' || true)"
echo "备份完成:$OUT($SIZE,$COUNT 张表有数据)"

# 表数量突然变少是"备份了一个空库"最早的信号 —— 而空库备份成功时
# 什么都不会报错
if [ "$COUNT" -lt 10 ]; then
    echo "只有 $COUNT 张表有数据,比预期少 —— 确认一下连的是不是对的库" >&2
    exit 1
fi

find "$DIR" -name 'lifein-*.dump' -mtime "+$KEEP" -delete

cat <<'NOTE'

提醒两件事:

1. **备份和主密钥不要放同一个地方。** .env 在服务器上,备份也在服务器上的话,
   一次拖库就两样都拿走了 —— 加密等于没做(R1)。
2. **这份备份还没有被还原过。** 按 scripts/restore-drill.md 走一遍,
   而且要定期重走 —— 一份从来没被还原过的备份,和没有备份的区别只有一个:
   前者让你以为自己有备份。
NOTE
