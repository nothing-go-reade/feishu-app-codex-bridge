# Example Raw Dify Audit Prompt

You are helping with a long-running SQL replacement task after an upstream Dify request timed out.

Return only JSON. Do not call any callback API yourself.

If you can safely produce a full replacement SQL, return:

```json
{
  "result": "full replacement SQL",
  "analysis": "why the replacement is safe",
  "callback": {
    "callbackUrl": "http://127.0.0.1:8080/example/raw-dify/callback",
    "taskId": "demo-task-001",
    "source": "FEISHU_APP_CODEX_BRIDGE"
  }
}
```

If the source table, field mapping, or SQL is incomplete, return an empty result:

```json
{
  "result": "",
  "analysis": "explain why this cannot be safely replaced",
  "callback": {
    "callbackUrl": "http://127.0.0.1:8080/example/raw-dify/callback",
    "taskId": "demo-task-001",
    "source": "FEISHU_APP_CODEX_BRIDGE"
  }
}
```

Task metadata:

```json
{
  "taskId": "demo-task-001",
  "sourceTable": "demo_raw.orders",
  "targetTable": "demo_dm.order_detail",
  "fieldMapping": {
    "order_id": "order_id",
    "buyer_id": "buyer_id",
    "seller_id": "seller_id"
  }
}
```

Original SQL:

```sql
select order_id, buyer_id, seller_id
from demo_raw.orders
where dt = '${date}'
```

