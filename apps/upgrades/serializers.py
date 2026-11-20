from rest_framework import serializers

from .models import UpgradeLog


class UpgradeLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = UpgradeLog
        fields = ["id", "version", "previous", "migrations", "backup", "started_at", "finished_at"]
