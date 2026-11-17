# frozen_string_literal: true
require_relative 'test_helper'
require_relative '../lib/traceai/auto/wire'
require_relative '../lib/traceai/auto/config'
# A hide switch someone set must never silently do nothing. For every FI_HIDE_*
# variable, its init option and its futureagi.toml setting: true, 1, yes and on
# hide; false, 0, no and off show; a blank value is not set; any other value
# hides, with one warning that names the variable and the accepted values and
# never prints the value. A hide line in the file is never dropped, however its
# value is written. A config file that is there but cannot be used says so once,
# by its path and never by its content. Other switches keep their own parsing.
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
      # Letter case and surrounding spaces do not matter in an init option either.
      config, stderr = read({}, {switch.to_sym => ' ON '})
      assert hides?(config, switch), "#{switch}: ' ON '"
      assert_empty stderr
      config, stderr = read({variable(switch) => 'on'}, {switch.to_sym => ' Off '})
      refute hides?(config, switch), "#{switch}: ' Off ' over the environment"
      assert_empty stderr
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
  def test_warning_lists_the_accepted_values
    _, stderr = read('FI_HIDE_INPUTS' => 'maybe')
    assert_equal 1, stderr.lines.length, stderr
    assert_includes stderr, 'true, 1, yes or on'
    assert_includes stderr, 'false, 0, no or off'
  end
  # With RUBYOPT the agent reads the environment when it loads, before the
  # application can call init. The warning is printed then, so it must not
  # promise more than is known: an init option that sets the switch decides.
  def test_warning_says_that_an_init_option_still_decides
    _, stderr = read('FI_HIDE_INPUTS' => 'maybe')
    assert_equal 1, stderr.lines.length, stderr
    assert_includes stderr, 'hidden unless an init option sets it'
    config, stderr = read({'FI_HIDE_INPUTS' => 'maybe'}, {hide_inputs: false})
    refute hides?(config, 'hide_inputs')
    assert_empty stderr
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
    # The value as written in the file => [hidden, warnings].
    {'true' => [true, 0], '"on"' => [true, 0], '" Yes "' => [true, 0], '1' => [true, 0], '"maybe"' => [true, 1], '2' => [true, 1],
     'false' => [false, 0], '"off"' => [false, 0], '0' => [false, 0], '""' => [false, 0],
     # A hide line is never dropped: a single-quoted string and a bare word are read as their text.
     "'on'" => [true, 0], "' Yes '" => [true, 0], 'on' => [true, 0], 'True' => [true, 0], 'TRUE' => [true, 0],
     'on # hide prompts' => [true, 0],
     "'off'" => [false, 0], 'off' => [false, 0], 'No' => [false, 0], "''" => [false, 0],
     "'maybe'" => [true, 1], 'maybe' => [true, 1], 'ture' => [true, 1],
     # So is a value the file reader cannot decode.
     '"on' => [true, 1]}.each do |value, (hidden, warnings)|
      File.write(file, "hide_inputs = #{value}\n")
      config, stderr = read('FI_CONFIG_FILE' => file)
      assert_equal hidden, hides?(config, 'hide_inputs'), value
      assert_equal warnings, stderr.lines.length, "#{value}: #{stderr}"
      refute_includes stderr, 'maybe'
    end
  ensure
    File.unlink(file) if file && File.exist?(file)
  end
  def test_every_switch_reads_its_config_file_setting
    file = File.join(ROOT, '.bundle/tmp', "hide-switch-every-#{Process.pid}.toml")
    SWITCHES.each_key do |switch|
      # One value that hides and one that shows, as a double-quoted string, a
      # single-quoted string and a bare word.
      {'"on"' => true, "'on'" => true, 'yes' => true, '"off"' => false, "'off'" => false, 'no' => false}.each do |value, hidden|
        File.write(file, "#{switch} = #{value}\n")
        config, stderr = read('FI_CONFIG_FILE' => file)
        assert_equal hidden, hides?(config, switch), "#{switch} = #{value}"
        assert_empty stderr, "#{switch} = #{value}"
      end
      File.write(file, "#{switch} = ture\n")
      config, stderr = read('FI_CONFIG_FILE' => file)
      assert hides?(config, switch), "#{switch} = ture"
      assert_equal 1, stderr.lines.length, stderr
      assert_includes stderr, variable(switch)
    end
  ensure
    File.unlink(file) if file && File.exist?(file)
  end
  # A config file that is there but cannot be used must say so: the hide
  # settings in it do nothing, and that must never be silent. One warning names
  # the file's path and what failed, never the file's content. What the file
  # sets does not change.
  def test_config_file_lines_that_are_not_utf8_warn_once_by_path_and_never_by_content
    file = File.join(ROOT, '.bundle/tmp', "hide-switch-not-utf8-#{Process.pid}.toml")
    # Two lines saved as Latin-1, around one line that is valid UTF-8.
    File.binwrite(file, "hide_inputs = true # PRIVATE_NOTE caf\xE9\nhide_outputs = true\nproject_name = \"PRIVATE_NAME_\xE9\"\n".b)
    config, stderr = read('FI_CONFIG_FILE' => file)
    assert_equal 1, stderr.lines.length, stderr
    assert_includes stderr, file
    assert_includes stderr, 'not valid UTF-8'
    refute_includes stderr, 'PRIVATE'
    assert_equal 1, config.warnings.length
    # The file is read as before: a line that is valid UTF-8 applies, the others are skipped.
    assert hides?(config, 'hide_outputs'), 'the line that is valid UTF-8'
    # A hide line is kept even when a comment on it is damaged: the switch someone set still hides.
    assert hides?(config, 'hide_inputs'), 'a hide line whose comment is not valid UTF-8'
    # Every read gives the same text, which is how the agent prints it once per process.
    again, = read('FI_CONFIG_FILE' => file)
    assert_equal config.warnings, again.warnings
  ensure
    File.unlink(file) if file && File.exist?(file)
  end
  def test_hide_line_with_a_damaged_byte_in_its_value_hides_and_warns_by_variable
    file = File.join(ROOT, '.bundle/tmp', "hide-switch-bad-value-#{Process.pid}.toml")
    File.binwrite(file, "hide_inputs = \"on\xE9\"\n".b)
    config, stderr = read('FI_CONFIG_FILE' => file)
    assert hides?(config, 'hide_inputs')
    assert_includes stderr, 'FI_HIDE_INPUTS'
    assert_includes stderr, file
    assert_equal 2, stderr.lines.length, stderr
  ensure
    File.unlink(file) if file && File.exist?(file)
  end
  def test_quoted_keys_in_the_config_file_are_hide_switches
    file = File.join(ROOT, '.bundle/tmp', "hide-switch-quoted-key-#{Process.pid}.toml")
    ['"hide_inputs" = true', "'hide_inputs' = true", '"hide_inputs" = "on"', "'hide_inputs' = off"].each do |line|
      File.write(file, line + "\n")
      config, stderr = read('FI_CONFIG_FILE' => file)
      assert_equal !line.end_with?('off'), hides?(config, 'hide_inputs'), line
      assert_empty stderr, line
    end
  ensure
    File.unlink(file) if file && File.exist?(file)
  end
  def test_triple_quotes_arrays_and_floats_in_the_config_file
    file = File.join(ROOT, '.bundle/tmp', "hide-switch-forms-#{Process.pid}.toml")
    {'"""off"""' => [false, 0], '"""on"""' => [true, 0], '["off"]' => [false, 0], '["on"]' => [true, 0],
     '1.0' => [true, 1], '0.0' => [true, 1]}.each do |value, (hidden, warnings)|
      File.write(file, "hide_inputs = #{value}\n")
      config, stderr = read('FI_CONFIG_FILE' => file)
      assert_equal hidden, hides?(config, 'hide_inputs'), value
      assert_equal warnings, stderr.lines.length, "#{value}: #{stderr}"
    end
  ensure
    File.unlink(file) if file && File.exist?(file)
  end
  # Reports show a hide switch by its resolved state, never by its raw text.
  def test_masked_report_shows_hide_switches_by_state
    config, = read('FI_HIDE_INPUTS' => 'maybe', 'FI_HIDE_OUTPUTS' => 'off', 'FI_HIDE_INPUT_TEXT' => 'ON')
    masked = config.masked
    assert_equal 'hide', masked['hide_inputs']
    assert_equal 'show', masked['hide_outputs']
    assert_equal 'hide', masked['hide_input_text']
    assert_equal 'unset', masked['hide_error_details']
    refute_includes masked.inspect, 'maybe'
  end
  def test_config_file_over_the_size_limit_warns_once_by_path_and_never_by_content
    file = File.join(ROOT, '.bundle/tmp', "hide-switch-too-large-#{Process.pid}.toml")
    File.write(file, "hide_inputs = true\n" + "# PRIVATE_NOTE #{'x' * 60}\n" * 1000)
    assert_operator File.size(file), :>, 65_536
    config, stderr = read('FI_CONFIG_FILE' => file)
    assert_equal 1, stderr.lines.length, stderr
    assert_includes stderr, file
    assert_includes stderr, '64 KiB'
    assert_includes stderr, 'none of its settings apply'
    refute_includes stderr, 'PRIVATE'
    assert_equal 1, config.warnings.length
    refute hides?(config, 'hide_inputs'), 'a file over the limit is still not read'
    # A file of exactly the limit is read and prints nothing.
    line = "hide_inputs = true\n"
    File.write(file, line + '#' * (65_536 - line.bytesize - 1) + "\n")
    assert_equal 65_536, File.size(file)
    config, stderr = read('FI_CONFIG_FILE' => file)
    assert hides?(config, 'hide_inputs'), 'a file of exactly the limit'
    assert_empty stderr
  ensure
    File.unlink(file) if file && File.exist?(file)
  end
  def test_config_file_that_cannot_be_read_warns_once_by_path_and_never_by_content
    file = File.join(ROOT, '.bundle/tmp', "hide-switch-unreadable-#{Process.pid}.toml")
    File.write(file, "hide_inputs = true # PRIVATE_NOTE\n")
    config = stderr = nil
    # The file is there, and reading it fails (as it does without the permission to read it).
    File.stub(:binread, ->(*) { raise Errno::EACCES }) { config, stderr = read('FI_CONFIG_FILE' => file) }
    assert_equal 1, stderr.lines.length, stderr
    assert_includes stderr, file
    assert_includes stderr, 'could not be read'
    assert_includes stderr, 'none of its settings apply'
    refute_includes stderr, 'PRIVATE'
    assert_equal 1, config.warnings.length
    refute hides?(config, 'hide_inputs'), 'a file that cannot be read still sets nothing'
  ensure
    File.unlink(file) if file && File.exist?(file)
  end
  def test_config_file_that_is_not_there_prints_nothing
    file = File.join(ROOT, '.bundle/tmp', "hide-switch-absent-#{Process.pid}.toml")
    refute File.exist?(file)
    # Named by the variable, and named by the init option.
    [[{'FI_CONFIG_FILE' => file}, {}], [{}, {config_file: file}]].each do |env, arguments|
      config, stderr = read(env, arguments)
      assert_empty stderr
      assert_empty config.warnings
      refute hides?(config, 'hide_inputs')
    end
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
