# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/ryan-yuuu/crypto-trading-arena/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                    |    Stmts |     Miss |   Cover |   Missing |
|------------------------ | -------: | -------: | ------: | --------: |
| arena/account\_store.py |      103 |        1 |     99% |        34 |
| arena/dashboard.py      |      163 |       73 |     55% |65, 84, 102, 105-107, 119-139, 142-143, 146-147, 190-273, 276-302 |
| arena/fees.py           |       27 |        0 |    100% |           |
| arena/models.py         |       55 |        0 |    100% |           |
| arena/price\_book.py    |       62 |       18 |     71% |     50-82 |
| arena/recorder.py       |       95 |        1 |     99% |       182 |
| arena/strategies.py     |        2 |        0 |    100% |           |
| arena/tools.py          |       87 |        4 |     95% |   160-165 |
| config.py               |       81 |        0 |    100% |           |
| **TOTAL**               |  **675** |   **97** | **86%** |           |


## Setup coverage badge

Below are examples of the badges you can use in your main branch `README` file.

### Direct image

[![Coverage badge](https://raw.githubusercontent.com/ryan-yuuu/crypto-trading-arena/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/ryan-yuuu/crypto-trading-arena/blob/python-coverage-comment-action-data/htmlcov/index.html)

This is the one to use if your repository is private or if you don't want to customize anything.

### [Shields.io](https://shields.io) Json Endpoint

[![Coverage badge](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/ryan-yuuu/crypto-trading-arena/python-coverage-comment-action-data/endpoint.json)](https://htmlpreview.github.io/?https://github.com/ryan-yuuu/crypto-trading-arena/blob/python-coverage-comment-action-data/htmlcov/index.html)

Using this one will allow you to [customize](https://shields.io/endpoint) the look of your badge.
It won't work with private repositories. It won't be refreshed more than once per five minutes.

### [Shields.io](https://shields.io) Dynamic Badge

[![Coverage badge](https://img.shields.io/badge/dynamic/json?color=brightgreen&label=coverage&query=%24.message&url=https%3A%2F%2Fraw.githubusercontent.com%2Fryan-yuuu%2Fcrypto-trading-arena%2Fpython-coverage-comment-action-data%2Fendpoint.json)](https://htmlpreview.github.io/?https://github.com/ryan-yuuu/crypto-trading-arena/blob/python-coverage-comment-action-data/htmlcov/index.html)

This one will always be the same color. It won't work for private repos. I'm not even sure why we included it.

## What is that?

This branch is part of the
[python-coverage-comment-action](https://github.com/marketplace/actions/python-coverage-comment)
GitHub Action. All the files in this branch are automatically generated and may be
overwritten at any moment.