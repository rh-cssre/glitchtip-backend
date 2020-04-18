from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives


def send_email_notification(notification):
    subject = "GlitchTip"
    issue_count = notification.issues.count()
    first_issue = notification.issues.all().first()
    if issue_count == 1:
        subject = f"GlitchTip error: {first_issue.title}"
    elif issue_count > 1:
        subject = f"GlitchTip {issue_count} errors including {first_issue.title}"

    text_content = "GlitchTip Errors\n"
    for issue in notification.issues.all():
        text_content += f"{issue.title}\n"
    html_content = "<h2>GlitchTip Errors</h2>\n"
    for issue in notification.issues.all():
        html_content += f"<div>{issue.title}<div>\n"

    User = get_user_model()
    users = User.objects.filter(team__projects__notification=notification)
    to = users.values_list("email", flat=True)
    msg = EmailMultiAlternatives(subject, text_content, to=to)
    msg.attach_alternative(html_content, "text/html")
    msg.send()
