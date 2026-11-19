# frozen_string_literal: true
module TraceAI
  module Auto
    module Wire
      # Output recognition is independent of retained text/usage and privacy.
      # Inspect decoded payloads without retaining audio or implying execution.
      module Milestones
        RESPONSE_DELTAS = %w[response.output_text.delta response.refusal.delta response.audio.transcript.delta response.audio.delta response.reasoning_text.delta response.reasoning_summary_text.delta response.function_call_arguments.delta response.mcp_call_arguments.delta response.code_interpreter_call_code.delta response.custom_tool_call_input.delta].freeze
        module_function
        def string?(value) = value.is_a?(String) && !value.empty?
        def arguments?(value) = string?(value) || (value.is_a?(Hash) && !value.empty?)
        def objects(value) = value.is_a?(Hash) ? [value] : (value.is_a?(Array) ? value.grep(Hash) : [])
        def function?(value)
          value.is_a?(Hash) && (string?(value['name']) || arguments?(value['arguments']))
        end
        def content?(value)
          return string?(value) if value.is_a?(String)
          objects(value).any? do |part|
            next false unless part['type'].nil? || %w[text thinking].include?(part['type'])
            string?(part['text']) || string?(part['thinking']) ||
              (part['type'] == 'thinking' && objects(part['thinking']).any? { |p| p['type'] == 'text' && string?(p['text']) })
          end
        end
        def output?(format, event)
          return false unless event.is_a?(Hash)
          case format
          when 'openai.chat'
            objects(event['choices']).any? do |choice|
              delta = choice['delta']; next false unless delta.is_a?(Hash)
              content?(delta['content']) || %w[refusal reasoning_content reasoning].any? { |key| string?(delta[key]) } ||
                (delta['audio'].is_a?(Hash) && %w[transcript data].any? { |key| string?(delta['audio'][key]) }) ||
                objects(delta['tool_calls']).any? { |call| function?(call['function']) } || function?(delta['function_call'])
            end
          when 'openai.completions'
            objects(event['choices']).any? { |choice| string?(choice['text']) }
          when 'openai.responses'
            return string?(event['delta']) if RESPONSE_DELTAS.include?(event['type'])
            return false unless event['type'] == 'response.output_item.added' && event['item'].is_a?(Hash)
            item = event['item']
            case item['type']
            when 'function_call', 'tool_call', 'custom_tool_call', 'mcp_call'
              function?(item) || function?(item['function']) || (item['type'] == 'custom_tool_call' && string?(item['input']))
            when 'code_interpreter_call' then string?(item['code'])
            else false
            end
          when 'anthropic.messages', 'bedrock.invoke'
            case event['type']
            when 'content_block_delta'
              delta = event['delta']; return false unless delta.is_a?(Hash)
              key = {'text_delta' => 'text', 'thinking_delta' => 'thinking', 'input_json_delta' => 'partial_json'}[delta['type']]
              key && string?(delta[key])
            when 'content_block_start'
              block = event['content_block']; return false unless block.is_a?(Hash)
              (block['type'] == 'text' && string?(block['text'])) || (block['type'] == 'thinking' && string?(block['thinking'])) ||
                (%w[tool_use server_tool_use mcp_tool_use].include?(block['type']) && string?(block['name']))
            else false
            end
          when 'gemini.generate_content'
            objects(event['candidates']).any? do |candidate|
              body = candidate['content']; next false unless body.is_a?(Hash)
              objects(body['parts']).any? do |part|
                call = part['functionCall']; audio = part['inlineData'] || part['inline_data']; code = part['executableCode']
                string?(part['text']) || (call.is_a?(Hash) && (string?(call['name']) || arguments?(call['args']))) ||
                  (code.is_a?(Hash) && string?(code['code'])) ||
                  (audio.is_a?(Hash) && (audio['mimeType'] || audio['mime_type']).to_s.start_with?('audio/') && string?(audio['data']))
              end
            end
          when 'bedrock.converse'
            delta = event.dig('contentBlockDelta', 'delta') || {}; start = event.dig('contentBlockStart', 'start') || {}
            string?(delta['text']) || string?(delta.dig('reasoningContent', 'text')) ||
              string?(delta.dig('toolUse', 'name')) || arguments?(delta.dig('toolUse', 'input')) || string?(start.dig('toolUse', 'name'))
          when 'cohere.chat'
            message = event.dig('delta', 'message') || {}
            case event['type'] || event['event_type']
            when 'content-start', 'content-delta' then content?(message['content'])
            when 'tool-call-start', 'tool-call-delta' then objects(message['tool_calls']).any? { |call| function?(call['function']) }
            when 'tool-plan-delta' then string?(message['tool_plan'])
            when 'text-generation' then string?(event['text'])
            when 'tool-calls-chunk'
              call = event['tool_call_delta'] || {}
              string?(event['text']) || string?(call['name']) || arguments?(call['parameters']) || string?(call['text'])
            when 'tool-calls-generation'
              objects(event['tool_calls']).any? { |call| string?(call['name']) || arguments?(call['parameters']) }
            else false
            end
          when 'ollama.chat'
            message = event['message']; return false unless message.is_a?(Hash)
            string?(message['content']) || string?(message['thinking']) || objects(message['tool_calls']).any? { |call| function?(call['function']) }
          when 'ollama.generate'
            string?(event['response']) || string?(event['thinking'])
          when 'cartesia.tts'
            event['type'] == 'chunk' && string?(event['data'])
          else false
          end
        rescue TypeError, NoMethodError
          false # Malformed provider shapes must not affect the application.
        end
        def first_text(format, frames, arrivals)
          index = 0
          frames.each do |event, end_offset|
            next unless output?(format, event)
            index += 1 while index < arrivals.size && arrivals[index][0] < end_offset
            return arrivals[index]&.last
          end
          nil
        end
      end
    end
  end
end
