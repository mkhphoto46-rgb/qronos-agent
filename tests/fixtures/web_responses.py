"""Recorded provider responses, captured 2026-08-27.

The DuckDuckGo pages are real HTML from ``html.duckduckgo.com``, trimmed to
three result blocks each and otherwise byte-for-byte as served. The structure —
nesting, attribute order, whitespace, entity escaping — is what the parser has
to survive, so it is preserved exactly rather than tidied.
"""

from __future__ import annotations


# A Persian query, which is the case that matters most: it is the only keyless
# HTTP provider that returned usable Persian results in testing.
DDG_PERSIAN_HTML = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <meta http-equiv="content-type" content="text/html; charset=UTF-8" />
  <title>&#1583;&#1587;&#1578;&#1607; &#1576;&#1606;&#1583;&#1740; &#1601;&#1575;&#1740;&#1604; at DuckDuckGo</title>
</head>
<body class="body--html">
  <div>
    <div class="serp__results">
      <div id="links" class="results">

            <div class="result results_links results_links_deep web-result ">
              <div class="links_main links_deep result__body">
                  <h2 class="result__title">
                    <a rel="nofollow" class="result__a"
href="https://salamdonya.com/tech/ways-to-organize-your-files">&#1587;&#1575;&#1586;&#1605;&#1575;&#1606;&#1583;&#1607;&#1740; &#1608; &#1605;&#1585;&#1578;&#1576; &#1603;&#1585;&#1583;&#1606; &#1601;&#1575;&#1740;&#1604;</a>
                  </h2>
                  <div class="result__extras">
                    <div class="result__extras__url">
                      <a class="result__url" href="https://salamdonya.com/tech/ways-to-organize-your-files">
                        salamdonya.com/tech/ways-to-organize-your-files
                      </a>
                    </div>
                  </div>
                    <a class="result__snippet" href="https://salamdonya.com/tech/ways-to-organize-your-files">&#1605;&#1585;&#1578;&#1576; &#1603;&#1585;&#1583;&#1606; <b>&#1601;&#1575;&#1740;&#1604;</b> &#1608; &#1575;&#1587;&#1606;&#1575;&#1583; &#1563; &#1576;&#1607;&#1578;&#1585;&#1740;&#1606; &#1585;&#1575;&#1607; &#1607;&#1575; &#1576;&#1585;&#1575;&#1740; &#1587;&#1575;&#1586;&#1605;&#1575;&#1606;&#1583;&#1607;&#1740; &#1608; <b>&#1583;&#1587;&#1578;&#1607;</b> <b>&#1576;&#1606;&#1583;&#1740;</b> &#1570;&#1606;&#1607;&#1575; &#1576;&#1575; 4 &#1585;&#1608;&#1588; &#1587;&#1575;&#1583;&#1607;.</a>
                <div class="clear"></div>
              </div>
            </div>

            <div class="result results_links results_links_deep web-result ">
              <div class="links_main links_deep result__body">
                  <h2 class="result__title">
                    <a rel="nofollow" class="result__a" href="https://fa.wikipedia.org/wiki/%D8%B3%D8%A7%D9%85%D8%A7%D9%86%D9%87_%D9%81%D8%A7%DB%8C%D9%84%E2%80%8C%D8%A8%D9%86%D8%AF%DB%8C">&#1587;&#1575;&#1605;&#1575;&#1606;&#1607; &#1601;&#1575;&#1740;&#1604;&#8204;&#1576;&#1606;&#1583;&#1740; - &#1608;&#1740;&#1705;&#1740;&#8204;&#1662;&#1583;&#1740;&#1575;</a>
                  </h2>
                  <div class="result__extras">
                    <div class="result__extras__url">
                      <a class="result__url" href="https://fa.wikipedia.org/wiki/foo">
                        fa.wikipedia.org/wiki/&#1587;&#1575;&#1605;&#1575;&#1606;&#1607;
                      </a>
                    </div>
                  </div>
                    <a class="result__snippet" href="https://fa.wikipedia.org/wiki/foo">&#1587;&#1575;&#1605;&#1575;&#1606;&#1607;&#8204;&#1607;&#1575;&#1740; &#1662;&#1585;&#1608;&#1606;&#1583;&#1607;&#8204;&#1575;&#1740; &#1605;&#1593;&#1605;&#1608;&#1604;&#1575;&#1611; &#1583;&#1575;&#1585;&#1575;&#1740; &#1601;&#1607;&#1585;&#1587;&#1578;&#1607;&#1575;&#1740; &#1585;&#1575;&#1607;&#1606;&#1605;&#1575; &#1607;&#1587;&#1578;&#1606;&#1583;.</a>
                <div class="clear"></div>
              </div>
            </div>

            <div class="result results_links results_links_deep web-result ">
              <div class="links_main links_deep result__body">
                  <h2 class="result__title">
                    <a rel="nofollow" class="result__a" href="https://blog.rayanekomak.com/show-all-file-and-sort-by-date/">&#1570;&#1605;&#1608;&#1586;&#1588; &#1605;&#1585;&#1578;&#1576; &#1587;&#1575;&#1586;&#1740; &#1601;&#1575;&#1740;&#1604; &#1607;&#1575;</a>
                  </h2>
                  <div class="result__extras">
                    <div class="result__extras__url">
                      <a class="result__url" href="https://blog.rayanekomak.com/show-all-file-and-sort-by-date/">
                        blog.rayanekomak.com/show-all-file-and-sort-by-date/
                      </a>
                    </div>
                  </div>
                    <a class="result__snippet" href="https://blog.rayanekomak.com/show-all-file-and-sort-by-date/">&#1585;&#1608;&#1588;&#8204;&#1607;&#1575;&#1740; &#1575;&#1589;&#1604;&#1740; &#1605;&#1585;&#1578;&#1576; &#1587;&#1575;&#1586;&#1740; &#1601;&#1575;&#1740;&#1604;&#8204;&#1607;&#1575; &#1583;&#1585; File Explorer &#1608;&#1740;&#1606;&#1583;&#1608;&#1586;.</a>
                <div class="clear"></div>
              </div>
            </div>

            <div class="nav-link">
              <form action="/html/" method="post">
                <input type="submit" class='btn btn--alt' value="Next" />
                <input type="hidden" name="q" value="&#1583;&#1587;&#1578;&#1607; &#1576;&#1606;&#1583;&#1740; &#1601;&#1575;&#1740;&#1604;" />
                <input type="hidden" name="s" value="10" />
              </form>
            </div>
      </div>
    </div>
  </div>
</body>
</html>
"""


# An English query. Includes a result with no snippet, which the parser must
# keep rather than discard: a title and URL are still usable evidence.
DDG_ENGLISH_HTML = """<!DOCTYPE html>
<html>
<head><title>python dataclass at DuckDuckGo</title></head>
<body class="body--html">
  <div class="serp__results">
    <div id="links" class="results">

          <div class="result results_links results_links_deep web-result ">
            <div class="links_main links_deep result__body">
                <h2 class="result__title">
                  <a rel="nofollow" class="result__a"
href="https://docs.python.org/3/library/dataclasses.html">dataclasses &mdash; Data Classes &mdash; Python 3.14.7 documentation</a>
                </h2>
                <div class="result__extras">
                  <div class="result__extras__url">
                    <a class="result__url" href="https://docs.python.org/3/library/dataclasses.html">
                      docs.python.org/3/library/dataclasses.html
                    </a>
                  </div>
                </div>
                  <a class="result__snippet" href="https://docs.python.org/3/library/dataclasses.html">Learn how to use the <b>@dataclass</b> decorator and functions to automatically add __init__(), __repr__(), __eq__(), and other methods to user-defined classes.</a>
              <div class="clear"></div>
            </div>
          </div>

          <div class="result results_links results_links_deep web-result ">
            <div class="links_main links_deep result__body">
                <h2 class="result__title">
                  <a rel="nofollow" class="result__a" href="https://stackoverflow.com/questions/47955263/what-are-data-classes">python - What are data classes?</a>
                </h2>
                <div class="result__extras">
                  <div class="result__extras__url">
                    <a class="result__url" href="https://stackoverflow.com/questions/47955263/what-are-data-classes">
                      stackoverflow.com/questions/47955263/what-are-data-classes
                    </a>
                  </div>
                </div>
              <div class="clear"></div>
            </div>
          </div>

          <div class="result results_links results_links_deep web-result ">
            <div class="links_main links_deep result__body">
                <h2 class="result__title">
                  <a rel="nofollow" class="result__a" href="https://realpython.com/python-data-classes/">Data Classes in Python (Guide) - Real Python</a>
                </h2>
                <div class="result__extras">
                  <div class="result__extras__url">
                    <a class="result__url" href="https://realpython.com/python-data-classes/">
                      realpython.com/python-data-classes/
                    </a>
                  </div>
                </div>
                  <a class="result__snippet" href="https://realpython.com/python-data-classes/">Learn how to use data classes, a feature in Python 3.7, to create simple and readable data structures with basic functionality.</a>
              <div class="clear"></div>
            </div>
          </div>

    </div>
  </div>
</body>
</html>
"""


# What throttling actually looks like: HTTP 200, a valid page, and no result
# blocks at all. There is no error message and no "no results" text, which is
# why it cannot be told apart from a query that genuinely matched nothing.
DDG_THROTTLED_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <link rel="canonical" href="https://duckduckgo.com/">
  <meta http-equiv="content-type" content="text/html; charset=UTF-8">
  <title>
        DuckDuckGo
    </title>
</head>
<body class="body--html">
  <a name="top" id="top"></a>
  <form action="/html/" method="post">
    <input type="text" name="state_hidden" id="state_hidden" />
  </form>
  <div>
    <div class="site-wrapper-border"></div>
    <div id="header" class="header cw header--html">
      <a title="DuckDuckGo" href="/html/" class="header__logo-wrap"></a>
    </div>
  </div>
  <div id="bottom_spacing2"></div>
</body>
</html>
"""


WIKIPEDIA_FA_JSON = """{
  "batchcomplete": "",
  "query": {
    "search": [
      {
        "ns": 0,
        "title": "\\u0633\\u0627\\u0645\\u0627\\u0646\\u0647 \\u0641\\u0627\\u06cc\\u0644\\u200c\\u0628\\u0646\\u062f\\u06cc",
        "snippet": "<span class=\\"searchmatch\\">\\u0633\\u0627\\u0645\\u0627\\u0646\\u0647</span> \\u067e\\u0631\\u0648\\u0646\\u062f\\u0647\\u200c\\u0627\\u06cc",
        "size": 12345,
        "wordcount": 900
      },
      {
        "ns": 0,
        "title": "\\u067e\\u0627\\u06cc\\u062a\\u0648\\u0646",
        "snippet": "\\u06cc\\u06a9 \\u0632\\u0628\\u0627\\u0646 \\u0628\\u0631\\u0646\\u0627\\u0645\\u0647\\u200c\\u0646\\u0648\\u06cc\\u0633\\u06cc",
        "size": 54321,
        "wordcount": 4000
      }
    ]
  }
}"""


WIKIPEDIA_EMPTY_JSON = """{
  "batchcomplete": "",
  "query": {
    "search": []
  }
}"""


STACKEXCHANGE_JSON = """{
  "items": [
    {
      "title": "Python dataclass from a nested dict",
      "link": "https://stackoverflow.com/questions/51564841",
      "answer_count": 5,
      "score": 42,
      "is_answered": true
    },
    {
      "title": "How can I make a python dataclass hashable?",
      "link": "https://stackoverflow.com/questions/52390576",
      "answer_count": 2,
      "score": 17,
      "is_answered": false
    }
  ],
  "has_more": true,
  "quota_max": 300,
  "quota_remaining": 297
}"""


MARGINALIA_JSON = """{
  "license": "CC-BY-NC-SA 4.0",
  "query": "python dataclass",
  "results": [
    {
      "url": "https://discuss.pytorch.org/t/issue-of-using-jit-with-python-dataclass/172875",
      "title": "Issue of using JIT with Python dataclass - PyTorch Forums",
      "description": "I am trying to use Python dataclass with PyTorch JIT."
    },
    {
      "url": "https://example.org/notes/dataclasses",
      "title": "Notes on dataclasses",
      "description": "A short write-up about dataclasses."
    }
  ]
}"""
