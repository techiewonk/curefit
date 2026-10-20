"""Context for the Home dashboard (Unfold calls this for the admin index page)."""
from . import services


def callback(request, context):
    context.update(home=services.home())
    return context
