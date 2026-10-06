# Supported firecrawl-py range. BaseInstrumentor.instrument() refuses to run
# (and logs an error) outside it, so this is a range, not the CI pin: the tests
# run on firecrawl-py==4.46.2, the first release this package was verified on.
_instruments = ("firecrawl-py >= 4.46.2, < 5",)
