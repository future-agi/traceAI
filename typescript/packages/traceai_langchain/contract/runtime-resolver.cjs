// Route every framework import (including transitive ones) to the selected
// consumer, never to the workspace's locked LangChain dependencies.
module.exports = (request, options) => options.defaultResolver(request,
  /^@langchain\/(core|openai)(\/|$)/.test(request)
    ? { ...options, basedir: process.env.LANGCHAIN_CONSUMER }
    : options);
