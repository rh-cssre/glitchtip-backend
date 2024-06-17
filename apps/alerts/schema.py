from typing import Annotated, Literal, Optional, Union

from ninja import Field, ModelSchema
from pydantic import HttpUrl

from glitchtip.schema import CamelSchema

from .constants import RecipientType
from .models import AlertRecipient, ProjectAlert


class EmailAlertRecipientIn(CamelSchema):
    recipient_type: Literal[RecipientType.EMAIL]
    url: Optional[Union[HttpUrl, Literal[""]]] = Field(default="")


class WebhookAlertRecipientIn(CamelSchema):
    recipient_type: Union[
        Literal[
            RecipientType.DISCORD,
            RecipientType.GENERAL_WEBHOOK,
            RecipientType.GOOGLE_CHAT,
        ]
    ]
    url: HttpUrl


AlertRecipientIn = Annotated[
    Union[EmailAlertRecipientIn, WebhookAlertRecipientIn],
    Field(discriminator="recipient_type"),
]


class AlertRecipientSchema(CamelSchema, ModelSchema):
    class Meta:
        model = AlertRecipient
        fields = ["id", "recipient_type", "url"]


class ProjectAlertIn(CamelSchema, ModelSchema):
    name: str = Field(default="")
    alert_recipients: Optional[list[AlertRecipientIn]] = Field(default_factory=list)

    class Meta:
        model = ProjectAlert
        fields = [
            "name",
            "timespan_minutes",
            "quantity",
            "uptime",
        ]


class ProjectAlertSchema(CamelSchema, ModelSchema):
    alert_recipients: list[AlertRecipientSchema] = Field(
        validation_alias="alertrecipient_set"
    )

    class Meta(ProjectAlertIn.Meta):
        fields = ["id"] + ProjectAlertIn.Meta.fields
