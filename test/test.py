
print('hello world')
def add(x: float, y: float) -> float:
    """Adds two numbers."""
    return x + y

def subtract(x: float, y: float) -> float:
    """Subtracts two numbers."""
    return x - y

# Added functions for multiplication, division, and logarithm.


def multiply(x: float, y: float) -> float:
    """Multiplies two numbers."""
    return x * y




def logarithm(x: float) -> float:
    """Calculates the natural logarithm of a number."""
    import math
    return math.log(x)

def divide(x: float, y: float) -> float:
    return x / y



print(add(5, 3))