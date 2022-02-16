from rest_framework import serializers


class StatsV2Serializer(serializers.Serializer):
    category = serializers.ChoiceField(choices=("error", "error"))
    interval = serializers.ChoiceField(
        choices=(("1d", "1 day"), ("1h", "1 hour"), ("1m", "1 minute")),
        default="1h",
        required=False,
    )
    project = serializers.ListField(
        child=serializers.IntegerField(min_value=-1), required=False
    )
    field = serializers.ChoiceField(
        choices=(
            ("sum(quantity)", "sum(quantity)"),
            ("sum(times_seen", "sum(times_seen"),
        ),
    )
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()

    def validate(self, data):
        start = data.get("start")
        end = data.get("end")
        interval = data.get("interval")

        series_quantity = (end - start).days
        if interval == "1h":
            series_quantity *= 24
        elif interval == "1m":
            series_quantity *= 1440

        if series_quantity > 1000:
            raise serializers.ValidationError({"end": "Too many intervals"})
        return data

