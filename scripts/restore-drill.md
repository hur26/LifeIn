# 恢复演练清单

[03 的 P4 硬门槛](../docs/03-roadmap.md#前置硬门槛不满足则不开放)第 3 条:

> 备份与恢复演练成功至少一次

**注意它要的不是"配了备份",是"恢复演练过"。** 一份从来没有被还原过的备份,
和没有备份的区别只有一个:**前者让你以为自己有备份**。

这份清单走一遍大约二十分钟。**做完在最后一节记一行**,那一行就是门槛的凭据。

---

## 演练要在另一个库上做

**绝不在生产库上还原。** 演练的目的是"确认备份能用",而在生产库上还原
只能确认一件事:你刚刚把生产数据覆盖了。

```bash
createdb lifein_drill
```

---

## 一、还原

```bash
pg_restore -d lifein_drill --no-owner --clean --if-exists \
    /var/backups/lifein/lifein-<日期>.dump
```

`--no-owner`:演练库的属主和生产不一样,不加会报一堆 `role does not exist`。

**报错要看完,不要只看最后一行。** `pg_restore` 会跳过失败的对象继续跑,
最后可能仍然是退出码 0 —— 而那时你还原出来的是一个缺表的库。

---

## 二、数据在不在

```bash
psql -d lifein_drill -c "
SELECT 'users' t, count(*) FROM users
UNION ALL SELECT 'raw_events', count(*) FROM raw_events
UNION ALL SELECT 'transactions', count(*) FROM transactions
UNION ALL SELECT 'facts', count(*) FROM facts
UNION ALL SELECT 'credentials', count(*) FROM credentials
UNION ALL SELECT 'todos', count(*) FROM todos;"
```

**和生产上的数字比一遍。** 差几条正常(备份是某个时刻的快照),
差一个数量级不正常。

---

## 三、迁移版本对不对

```bash
psql -d lifein_drill -c "SELECT version_num FROM alembic_version;"
```

**它必须和代码里的最新一版一致。** 对不上意味着这份备份是老结构的,
而拿它还原之后代码跑不起来 —— 这一条比数据条数更容易被忽略。

---

## 四、凭据解得开(**最要紧的一步**)

前三步验的是"数据还在",这一步验的是"**数据还能用**"。

```bash
MASTER_KEY=<和备份同一时期的那把> \
DATABASE_URL=postgresql+psycopg://.../lifein_drill \
    python -m lifein.admin key-status --user <某个 uuid>
```

解不开的话,还原出来的库里那些凭据全是废的 —— 而症状是
**"还原完了但采不到邮件"**,和一个网络问题长得一模一样。

> **这一步是 ADR-022 那句"迁移当天最容易翻车的一条"的演练版:**
> `MASTER_KEY` 与 `MASTER_KEY_VERSION` 不跟着搬,`credentials` 里所有凭据
> 全部解不开。信封的 AAD 绑了 `user_id` 与 `kind`,密钥不对就是
> `DecryptError`,不是"读出乱码"。

---

## 五、跑得起来

```bash
DATABASE_URL=postgresql+psycopg://.../lifein_drill python -m lifein --check
```

启动自检过了就算完。**不用真的让它跑起来** —— 那样会往真实的通道发消息,
而演练不该打扰任何人。

`--check` 做三件事:配置校验、库版本比对、装配(工具注册与 agent 白名单)。
**它只看不改** —— 哪怕 `SCHEMA_AUTO_UPGRADE` 开着也不会把演练库升级
([ADR-030](../docs/04-tech-decisions.md#adr-030--库版本落后由进程自己发现并在空闲时段自己补上))。
退出码 0 才算过;库版本对不上会退 1 并写清差几个版本。

> **这一步以前是跑不起来的。** 这份清单一直写着 `--check`,而那个参数
> 直到 2026-09-10 才真的存在 —— 演练走到第五步会撞上
> `unrecognized arguments: --check`。**一份没被走过的清单,和没有清单的
> 区别只在于你以为自己有清单**(和这份文件开头那句关于备份的话是同一件事)。

---

## 六、收尾

```bash
dropdb lifein_drill
```

**演练库要删掉。** 留着的话,某天有人把 `DATABASE_URL` 指错,
写进去的东西谁都不会发现。

---

## 演练记录

每次演练在这里加一行。**这份表就是门槛的凭据** ——
"我记得做过"不算数。

| 日期 | 用的哪份备份 | 五步都过了吗 | 花了多久 | 发现了什么 |
| --- | --- | --- | --- | --- |
| | | | | |

> **一次都没有的时候这张表是空的,而空表就是"这条门槛没过"。**
> 不要因为"反正快了"就先开放 —— 03 那句"不满足则不开放"里的
> "不满足"包括这一条。
