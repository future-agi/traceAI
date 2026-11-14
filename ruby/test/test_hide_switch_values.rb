# frozen_string_literal: true
require_relative 'test_helper'
require_relative '../lib/traceai/auto/wire'
require_relative '../lib/traceai/auto/config'
# A hide switch someone set must never silently do nothing. For every FI_HIDE_*
# variable, its init option and its futureagi.toml setting: true, 1, yes and on
# hide; false, 0, no and off show; a blank value is not set; any other value
# hides, with one warning that names the variable and never prints the value.
# Other switches keep their own parsing.
class HideSwitchValuesTest < Minitest::Test
  A = TraceAI::Auto
  P = TraceAI::Auto::Privacy
  TEXT = 'PRIVATE_HIDE_SWITCH_TEXT'
  # One attribute that each switch hides when it is the only switch set.
  SWITCHES = {
    'hide_inputs' => 'input.value', 'hide_input_messages' => 'input.value', 'hide_input_text' => 'input.value',
    'hide_input_images' => 'input.value', 'hide_llm_invocation_parameters' => 'input.value',
    'hide_outputs' => 'output.value', 'hide_output_messages' => 'output.value', 'hide_output_text' => 'output.value',
    'hide_embedding_vectors' => 'embedding.embeddings.0.embedding.vector', 'hide_error_details' => 'error.message'
  }.freeze
  ATTRIBUTES = SWITCHES.values.uniq.to_h { |key| [key, TEXT] }.freeze
  def read(env = {}, arguments = {})
    config = nil
    _, stderr = capture_io { config = A::Config.new(arguments, env: env) }
    [config, stderr]
  end
  def hides?(config, switch) = P.mask(ATTRIBUTES, config)[SWITCHES.fetch(switch)] != TEXT
  def variable(switch) = "FI_#{switch.upcase}"
  def test_true_values_hide
    ['true', 'on', 'yes', '1', ' TRUE ', 'On'].each do |value|
      config, stderr = read('FI_HIDE_INPUTS' => value)
      assert hides?(config, 'hide_inputs'), value.inspect
      assert P.hide_errors?(config), value.inspect
      assert_empty stderr, value.inspect
    end
  end
  def test_false_values_show
    ['false', 'off', 'no', '0', ' OFF '].each do |value|
      config, stderr = read('FI_HIDE_INPUTS' => value)
      refute hides?(config, 'hide_inputs'), value.inspect
      refute P.hide_errors?(config), value.inspect
      assert_empty stderr, value.inspect
    end
  end
  def test_blank_value_is_not_set
    file = File.join(ROOT, '.bundle/tmp', "hide-switch-blank-#{Process.pid}.toml")
    File.write(file, "hide_inputs = true\n")
    ['', '   '].each do |value|
      config, stderr = read('FI_HIDE_INPUTS' => value)
      refute hides?(config, 'hide_inputs'), value.inspect
      assert_empty stderr, value.inspect
      # A blank value from a higher source does not switch off what a lower source hides.
      config, = read('FI_CONFIG_FILE' => file, 'FI_HIDE_INPUTS' => value)
      assert hides?(config, 'hide_inputs'), "blank environment value #{value.inspect} over the config file"
      config, = read({'FI_HIDE_INPUTS' => 'true'}, {hide_inputs: value})
      assert hides?(config, 'hide_inputs'), "blank init option #{value.inspect} over the environment"
    end
    config, = read({'FI_HIDE_INPUTS' => 'true'}, {hide_inputs: nil})
    assert hides?(config, 'hide_inputs'), 'nil init option over the environment'
  ensure
    File.unlink(file) if file && File.exist?(file)
  end
  def test_every_switch_reads_on_and_off
    SWITCHES.each_key do |switch|
      config, stderr = read(variable(switch) => 'on')
      assert hides?(config, switch), "#{variable(switch)}=on"
      assert_empty stderr
      config, stderr = read(variable(switch) => 'off')
      refute hides?(config, switch), "#{variable(switch)}=off"
      assert_empty stderr
      # The init option follows the same rule, as text or as a real boolean.
      assert hides?(read({}, {switch.to_sym => 'on'}).first, switch), "#{switch}: 'on'"
      assert hides?(read({}, {switch.to_sym => true}).first, switch), "#{switch}: true"
      refute hides?(read({variable(switch) => 'on'}, {switch.to_sym => false}).first, switch), "#{switch}: false over the environment"
    end
  end
  def test_unrecognised_value_hides_with_one_warning_that_names_the_variable_and_not_the_value
    SWITCHES.each_key do |switch|
      config, stderr = read(variable(switch) => 'maybe')
      assert hides?(config, switch), "#{variable(switch)}=maybe"
      assert_equal 1, stderr.lines.length, stderr
      assert_includes stderr, variable(switch)
      refute_includes stderr, 'maybe'
      assert_equal 1, config.warnings.length
    end
  end
  def test_error_details_follow_the_content_switches_unless_set_to_a_recognised_value
    # A recognised value wins over the content switches.
    refute P.hide_errors?(read('FI_HIDE_INPUTS' => 'true', 'FI_HIDE_ERROR_DETAILS' => 'off').first)
    assert P.hide_errors?(read('FI_HIDE_INPUTS' => 'false', 'FI_HIDE_ERROR_DETAILS' => 'on').first)
    # A blank one follows them.
    assert P.hide_errors?(read('FI_HIDE_INPUTS' => 'true', 'FI_HIDE_ERROR_DETAILS' => '  ').first)
    refute P.hide_errors?(read('FI_HIDE_INPUTS' => 'false', 'FI_HIDE_ERROR_DETAILS' => '').first)
    # Any other value hides error details, with one warning.
    config, stderr = read('FI_HIDE_INPUTS' => 'false', 'FI_HIDE_ERROR_DETAILS' => 'enabled')
    assert P.hide_errors?(config)
    assert_equal 1, stderr.lines.length, stderr
    assert_includes stderr, 'FI_HIDE_ERROR_DETAILS'
    refute_includes stderr, 'enabled'
  end
  def test_config_file_values_follow_the_same_rule
    file = File.join(ROOT, '.bundle/tmp', "hide-switch-values-#{Process.pid}.toml")
    {'true' => true, '"on"' => true, '" Yes "' => true, '1' => true, '"maybe"' => true, '2' => true,
     'false' => false, '"off"' => false, '0' => false, '""' => false}.each do |value, hidden|
      File.write(file, "hide_inputs = #{value}\n")
      config, stderr = read('FI_CONFIG_FILE' => file)
      assert_equal hidden, hides?(config, 'hide_inputs'), value
      assert_equal(['"maybe"', '2'].include?(value) ? 1 : 0, stderr.lines.length, "#{value}: #{stderr}")
      refute_includes stderr, 'maybe'
    end
  ensure
    File.unlink(file) if file && File.exist?(file)
  end
  def test_other_switches_keep_their_own_parsing
    ['on', 'yes', '1', ' TRUE ', 'maybe'].each do |value|
      config, stderr = read('FI_WIRE_CAPTURE' => value, 'FI_AUTO_DEBUG' => value, 'OTEL_SDK_DISABLED' => value)
      refute config.truthy?('wire_capture'), value.inspect
      refute config.truthy?('auto_debug'), value.inspect
      refute config.truthy?('sdk_disabled'), value.inspect
      SWITCHES.each_key { |switch| refute hides?(config, switch), "#{value.inspect} turned on #{switch}" }
      assert_empty stderr, value.inspect
    end
    config, = read('FI_WIRE_CAPTURE' => 'TRUE', 'FI_AUTO_DEBUG' => 'True', 'OTEL_SDK_DISABLED' => 'true')
    assert config.truthy?('wire_capture'); assert config.truthy?('auto_debug'); assert config.truthy?('sdk_disabled')
  end
end
