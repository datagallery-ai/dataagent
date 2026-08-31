# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
from dataagent.actions.environment.env import Env


class ArithmeticEnv(Env):
    """
    Environment providing basic arithmetic operations.

    This environment provides four fundamental mathematical operations:
    addition, subtraction, multiplication, and division.

    Example:
        >>> env = ArithmeticEnv()
        >>> env.tools['add'](5, 3)
        8
        >>> env.tools['multiply'](4, 7)
        28
        >>> env.tools['divide'](10, 2)
        5.0
    """

    def __init__(self):
        """
        Initialize the arithmetic environment.

        Args:
            precision: Number of decimal places to round results to (default: 2)
        """
        super().__init__()

    @Env.tool
    def add(self, a: float, b: float) -> float:
        """
        Add two numbers.

        Args:
            a: First number
            b: Second number

        Returns:
            The sum of a and b

        Example:
            >>> add(5, 3)
            8.0
            >>> add(1.5, 2.7)
            4.2
        """
        return a + b

    @Env.tool
    def subtract(self, a: float, b: float) -> float:
        """
        Subtract b from a.

        Args:
            a: Number to subtract from
            b: Number to subtract

        Returns:
            The difference (a - b)

        Example:
            >>> subtract(10, 3)
            7.0
            >>> subtract(5.5, 2.3)
            3.2
        """
        return a - b

    @Env.tool
    def multiply(self, a: float, b: float) -> float:
        """
        Multiply two numbers.

        Args:
            a: First number
            b: Second number

        Returns:
            The product of a and b

        Example:
            >>> multiply(4, 7)
            28.0
            >>> multiply(2.5, 3.2)
            8.0
        """
        return a * b

    @Env.tool
    def divide(self, a: float, b: float) -> float:
        """
        Divide a by b.

        Args:
            a: Dividend (number to be divided)
            b: Divisor (number to divide by)

        Returns:
            The quotient (a / b), or NaN if b is zero

        Example:
            >>> divide(10, 2)
            5.0
            >>> divide(7, 3)
            2.33
            >>> divide(5, 0)
            nan
        """
        if b == 0:
            return float("nan")
        return a / b
