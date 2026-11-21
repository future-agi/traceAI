# frozen_string_literal: true
require_relative 'test_helper'
require 'minitest/mock'
require 'base64'
require_relative '../lib/traceai/auto/agent'
require_relative '../lib/traceai/auto/privacy'
require_relative '../lib/traceai/auto/export'

# Follow-ups to the span work bound: media context is read from the attributes as they came, a span normalises its secrets
# once, the operation name is the one the attribute map keeps, a registered secret protects the valid runs of a cut
# character, the fallback applies the hide settings by the original names, and the documentation states what is kept.
class SpanWorkBoundFollowupsTest < Minitest::Test
  P = TraceAI::Auto::Privacy
  PLACEHOLDER = P::REDACTED
  # What a hide setting leaves in place of a value it names.
  MASKED = '__REDACTED__'
  ROOT = File.expand_path('..', __dir__)
  # Not a recognised file signature: it is a payload only by the field it sits in.
  PAYLOAD = Base64.strict_encode64(Random.new(7).bytes(300))
  OMITTED = '<omitted: 400 base64 chars>'

  def record_with(attributes, events: [], links: [])
    memory = OpenTelemetry::SDK::Trace::Export::InMemorySpanExporter.new
    provider = OpenTelemetry::SDK::Trace::TracerProvider.new
    provider.add_span_processor(OpenTelemetry::SDK::Trace::Export::SimpleSpanProcessor.new(memory))
    span = provider.tracer('app').start_span('followup-span', links: links)
    events.each { |attrs| span.add_event('e', attributes: attrs) }
    span.finish
    record = memory.finished_spans.first
    record.attributes = attributes
    record.total_recorded_attributes = attributes.size
    record
  end

  def snapshot(record, settings = {}, secrets = [])
    config = TraceAI::Auto::Config.new(settings, env: {})
    TraceAI::Auto.stub(:secret_values, []) do
      P.snapshot(record, TraceAI::Auto::ExportResource.new({}), config, secrets)
    end
  end

  def arguments
    JSON.generate({'content' => PAYLOAD})
  end

  # ---- 1. media context comes from the attributes as they came ----------------------------------------------------

  def test_a_payload_beside_a_hidden_mime_type_is_still_omitted_in_a_span_attribute
    record = record_with({'gen_ai.tool.call.arguments' => arguments, 'gen_ai.tool.call.mime_type' => 'application/pdf'})
    copy = snapshot(record, {'hide_outputs' => true})
    assert_equal MASKED, copy.attributes['gen_ai.tool.call.mime_type'], 'the mime type is hidden'
    assert_equal JSON.generate({'content' => OMITTED}), copy.attributes['gen_ai.tool.call.arguments']
    refute_includes JSON.generate(copy.attributes), PAYLOAD
  end

  def test_the_same_payload_with_nothing_hidden_is_omitted_too
    record = record_with({'gen_ai.tool.call.arguments' => arguments, 'gen_ai.tool.call.mime_type' => 'application/pdf'})
    assert_equal JSON.generate({'content' => OMITTED}), snapshot(record).attributes['gen_ai.tool.call.arguments']
  end

  def test_a_payload_beside_a_hidden_mime_type_is_still_omitted_in_an_event_attribute
    attrs = {'gen_ai.tool.call.arguments' => arguments, 'gen_ai.tool.call.mime_type' => 'application/pdf'}
    copy = snapshot(record_with({}, events: [attrs]), {'hide_outputs' => true})
    event = copy.events.first.attributes
    assert_equal MASKED, event['gen_ai.tool.call.mime_type']
    assert_equal JSON.generate({'content' => OMITTED}), event['gen_ai.tool.call.arguments']
    refute_includes JSON.generate(event), PAYLOAD
  end

  def test_a_payload_beside_a_hidden_mime_type_is_still_omitted_in_a_link_attribute
    context = OpenTelemetry::Trace::SpanContext.new
    attrs = {'gen_ai.tool.call.arguments' => arguments, 'gen_ai.tool.call.mime_type' => 'application/pdf'}
    copy = snapshot(record_with({}, links: [OpenTelemetry::Trace::Link.new(context, attrs)]), {'hide_outputs' => true})
    link = copy.links.first.attributes
    assert_equal MASKED, link['gen_ai.tool.call.mime_type']
    assert_equal JSON.generate({'content' => OMITTED}), link['gen_ai.tool.call.arguments']
    refute_includes JSON.generate(link), PAYLOAD
  end

  # ---- 3. past the bound a media string keeps its own placeholder ------------------------------------------------

  def test_past_the_span_bound_a_media_string_keeps_its_omission_text_and_other_values_are_the_placeholder
    registry = Array.new(1024) { |i| format('registered-credential-%04d-', i) + ('k' * 180) }
    filler = ('x' + PLACEHOLDER) * 100
    attributes = (0...200).to_h { |i| ["app.value.#{i}", filler] }.merge('app.late.text' => 'hello', 'app.late.image' => "data:image/png;base64,#{PAYLOAD}")
    copy = snapshot(record_with(attributes), {}, registry)
    assert_equal OMITTED, copy.attributes[PLACEHOLDER], 'the later names share one entry, holding the last value'
    text_last = snapshot(record_with(attributes.merge('app.last.text' => 'hello')), {}, registry)
    assert_equal PLACEHOLDER, text_last.attributes[PLACEHOLDER]
  end

  def test_the_readme_and_the_changelog_say_that_a_media_string_past_the_bound_keeps_its_omission_text
    readme = File.read(File.join(ROOT, 'README.md'))
    changelog = File.read(File.join(ROOT, 'CHANGELOG.md'))
    limitation = readme[/Matching known secrets that overlap.*?(?=\n- Not covered)/m]
    refute_nil limitation
    assert_includes limitation, '<omitted: N base64 chars>'
    entry = changelog.lines.find { |line| line.include?('every later attribute name is replaced with the placeholder') }
    refute_nil entry
    assert_includes entry, '<omitted: N base64 chars>'
  end

  # ---- 4. a span normalises its secrets once ----------------------------------------------------------------------

  def test_a_span_normalises_the_secrets_once
    calls = 0
    original = P.method(:normalize_secrets)
    counted = ->(secrets) { calls += 1; original.call(secrets) }
    event = {'a.event.attribute' => 'a value'}
    record = record_with({'a.b' => 'a value', 'c.d' => 'another'}, events: [event, event])
    P.stub(:normalize_secrets, counted) { snapshot(record, {}, ['registered-credential-0001-key']) }
    assert_equal 1, calls
  end

  # ---- 5. the operation name is the one the attribute map keeps ---------------------------------------------------

  def test_the_operation_name_is_the_last_of_the_names_that_read_the_same
    attributes = {'gen_ai.operation.name' => 'text_to_speech', :'gen_ai.operation.name' => 'speech_to_text'}
    assert_equal 'speech_to_text', P.operation_name(attributes)
    named = P.hidden_by_name(attributes, TraceAI::Auto::Config.new({}, env: {}), 'speech_to_text')
    assert_equal 'speech_to_text', named['gen_ai.operation.name']
  end

  def test_a_transcript_is_hidden_by_the_operation_name_the_exported_attributes_carry
    attributes = {'gen_ai.operation.name' => 'text_to_speech', :'gen_ai.operation.name' => 'speech_to_text', 'gen_ai.audio.transcript' => 'OUTPUT-SIDE-TEXT'}
    copy = snapshot(record_with(attributes), {'hide_outputs' => true})
    refute_includes JSON.generate(copy.attributes), 'OUTPUT-SIDE-TEXT'
  end

  # ---- 6. a registered secret protects the valid runs of a cut character ------------------------------------------

  def test_a_registered_secret_cut_through_a_multibyte_character_still_protects_its_valid_runs
    registry = TraceAI::Auto::SecretRegistry.new
    assert registry.register("\xFFRUN-ONE-123456\xFERUN-TWO-654321\xFF".b)
    text = 'a RUN-ONE-123456 b RUN-TWO-654321 c'
    assert_equal "a #{PLACEHOLDER} b #{PLACEHOLDER} c", P.scrub(text, registry.snapshot)
  end

  # ---- 7. the fallback applies the hide settings by the original names ------------------------------------------

  # A protected usage key that a hide setting removes by its name; a registered secret inside the name changes it.
  VECTOR_KEY = 'gen_ai.usage.embedding.vector'

  def failed_admission(attributes)
    record = record_with(attributes)
    record.instance_variable_set(:@traceai_media, {failed: true})
    record
  end

  def test_the_fallback_removes_what_a_hide_setting_names_even_when_a_secret_changes_the_name
    copy = snapshot(failed_admission({VECTOR_KEY => 0.5}), {'hide_embedding_vectors' => true}, ['embedding'])
    refute_includes copy.attributes.values, 0.5, copy.attributes.inspect
  end

  def test_the_fallback_keeps_the_number_when_nothing_is_hidden
    copy = snapshot(failed_admission({VECTOR_KEY => 0.5}), {}, ['embedding'])
    assert_includes copy.attributes.values, 0.5, copy.attributes.inspect
  end
end
