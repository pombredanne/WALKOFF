import json
from functools import wraps

from core.helpers import get_function_arg_names


class ActionResult(object):
    def __init__(self, result, status):
        self.result = result
        self.status = status

    def as_json(self):
        try:
            json.dumps(self.result)
            return {"result": self.result, "status": self.status}
        except TypeError:
            return {"result": str(self.result), "status": self.status}

    def __eq__(self, other):
        return self.__dict__ == other.__dict__


def format_result(result):
    if not isinstance(result, tuple):
        return ActionResult(result, 'Success')
    else:
        return ActionResult(*result)


def tag(func, tag_name):
    setattr(func, tag_name, True)


def action(func):
    """
    Decorator used to tag a method or function as an action

    Args:
        func (func): Function to tag
    Returns:
        (func) Tagged function
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        return format_result(func(*args, **kwargs))

    tag(wrapper, 'action')
    wrapper.__arg_names = get_function_arg_names(func)
    return wrapper


def condition(func):
    """
    Decorator used to tag a method or function as a condition

    Args:
        func (func): Function to tag
    Returns:
        (func) Tagged function
    """
    tag(func, 'condition')
    return func


def transform(func):
    """
    Decorator used to tag a method or function as a transform

    Args:
        func (func): Function to tag
    Returns:
        (func) Tagged function
    """
    tag(func, 'transform')
    return func
