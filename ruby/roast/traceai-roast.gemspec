# frozen_string_literal: true

require_relative "lib/traceai/roast/version"

Gem::Specification.new do |spec|
  spec.name = "traceai-roast"
  spec.version = TraceAI::Roast::VERSION
  spec.summary = "OpenTelemetry workflow and cog spans for Shopify Roast"
  spec.authors = ["Future AGI"]
  spec.license = "Apache-2.0"
  spec.homepage = "https://github.com/future-agi/traceAI"
  spec.required_ruby_version = ">= 3.3"
  spec.files = Dir["lib/**/*.rb", "README.md", "LICENSE"]
  spec.require_paths = ["lib"]
  spec.add_dependency "roast-ai", ">= 1.1.0", "< 2.0"
  spec.add_dependency "opentelemetry-api", ">= 1.0", "< 2.0"
end
