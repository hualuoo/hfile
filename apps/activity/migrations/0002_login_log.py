"""The 登录日志 the 操作日志 replaces: each row of accounts.LoginLog becomes an entry, a login or a failed one, with
its time, IP, location and browser (as apps/accounts/loginlog.py writes them now). accounts.0004 then drops
that table."""

from django.db import migrations

CHUNK = 1000


def entry(ActivityLog, row):
    detail = {"source": row["source"]} if row["source"] else {}
    common = {"created_at": row["created_at"], "ip": row["ip"], "ip_location": row["ip_location"],
              "user_agent": row["user_agent"]}
    account = row["user__username"]
    if row["result"] == "success":
        return ActivityLog(kind="login", actor_id=row["user_id"], actor_name=account or row["username"],
                           detail=detail, **common)
    detail["reason"] = row["reason"]
    if account and row["username"] and row["username"] != account:
        detail["username"] = row["username"]
    return ActivityLog(kind="login_failed", target_id=row["user_id"], target_name=account or row["username"],
                       detail=detail, **common)


def move(apps, schema_editor):
    LoginLog = apps.get_model("accounts", "LoginLog")
    ActivityLog = apps.get_model("activity", "ActivityLog")
    rows = LoginLog.objects.order_by("id").values(
        "created_at", "username", "user_id", "user__username", "ip", "ip_location", "user_agent", "result", "reason",
        "source")
    batch = []
    for row in rows.iterator(chunk_size=CHUNK):
        batch.append(entry(ActivityLog, row))
        if len(batch) >= CHUNK:
            ActivityLog.objects.bulk_create(batch)
            batch = []
    ActivityLog.objects.bulk_create(batch)


class Migration(migrations.Migration):
    dependencies = [("activity", "0001_initial"), ("accounts", "0003_ip_locations")]
    operations = [migrations.RunPython(move, migrations.RunPython.noop)]
