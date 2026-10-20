"""Month arithmetic shared by the report modules (every month is the date of its first day)."""
import datetime as dt


def month_of(day):
    return day.replace(day=1)


def next_month(month):
    return (month + dt.timedelta(days=32)).replace(day=1)


def label(month):
    return month.strftime("%b-%y")


def days_in(month):
    return (next_month(month) - month).days
