"""Context for the Home dashboard (Unfold calls this for the admin index page)."""
from scraper import selectors


def callback(request, context):
    context.update(home=selectors.home())
    return context
