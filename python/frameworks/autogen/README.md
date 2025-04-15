# Autogen OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the Autogen framework. It enables tracing and monitoring of applications built with Autogen.

## Installation

1. **Install traceAI Autogen**

```bash
pip install traceAI-autogen
```


### Set Environment Variables
Set up your environment variables to authenticate with both FutureAGI.

```python
import os

os.environ["FI_API_KEY"] = FI_API_KEY
os.environ["FI_SECRET_KEY"] = FI_SECRET_KEY
```

## Quickstart

### Register Tracer Provider
Set up the trace provider to establish the observability pipeline. The trace provider:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="autogen_app",
    project_version_name="v1",
    session_name="chat-bot"
)
```

### Configure Autogen Instrumentation
Instrument the Autogen client to enable telemetry collection. This step ensures that all interactions with the Autogen SDK are tracked and monitored.

```python
from traceai_autogen import AutogenInstrumentor

AutogenInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create Autogen Components
Set up your Autogen client with built-in observability.

```python
llm_config = {
    "config_list": [{"model": "gpt-3.5-turbo", "api_key": os.environ.get('OPENAI_API_KEY')}],
    "cache_seed": 0,  # seed for reproducibility
    "temperature": 0,  # temperature to control randomness
}

LEETCODE_QUESTION = """
Title: Two Sum

Given an array of integers nums and an integer target, return indices of the two numbers such that they add up to target. You may assume that each input would have exactly one solution, and you may not use the same element twice. You can return the answer in any order.

Example 1:
Input: nums = [2,7,11,15], target = 9
Output: [0,1]
Explanation: Because nums[0] + nums[1] == 9, we return [0, 1].

Example 2:
Input: nums = [3,2,4], target = 6
Output: [1,2]

Example 3:
Input: nums = [3,3], target = 6
Output: [0,1]

Constraints:

2 <= nums.length <= 104
-109 <= nums[i] <= 109
-109 <= target <= 109
Only one valid answer exists.

Follow-up: Can you come up with an algorithm that is less than O(n2) time complexity?
"""

# create an AssistantAgent named "assistant"

SYSTEM_MESSAGE = """You are a helpful AI assistant.
Solve tasks using your coding and language skills.
In the following cases, suggest python code (in a python coding block) or shell script (in a sh coding block) for the user to execute.
1. When you need to collect info, use the code to output the info you need, for example, browse or search the web, download/read a file, print the content of a webpage or a file, get the current date/time, check the operating system. After sufficient info is printed and the task is ready to be solved based on your language skill, you can solve the task by yourself.
2. When you need to perform some task with code, use the code to perform the task and output the result. Finish the task smartly.
Solve the task step by step if you need to. If a plan is not provided, explain your plan first. Be clear which step uses code, and which step uses your language skill.
When using code, you must indicate the script type in the code block. The user cannot provide any other feedback or perform any other action beyond executing the code you suggest. The user can't modify your code. So do not suggest incomplete code which requires users to modify. Don't use a code block if it's not intended to be executed by the user.
If you want the user to save the code in a file before executing it, put # filename: <filename> inside the code block as the first line. Don't include multiple code blocks in one response. Do not ask users to copy and paste the result. Instead, use 'print' function for the output when relevant. Check the execution result returned by the user.
If the result indicates there is an error, fix the error and output the code again. Suggest the full code instead of partial code or code changes. If the error can't be fixed or if the task is not solved even after the code is executed successfully, analyze the problem, revisit your assumption, collect additional info you need, and think of a different approach to try.
When you find an answer, verify the answer carefully. Include verifiable evidence in your response if possible.

Additional requirements:
1. Within the code, add functionality to measure the total run-time of the algorithm in python function using "time" library.
2. Only when the user proxy agent confirms that the Python script ran successfully and the total run-time (printed on stdout console) is less than 50 ms, only then return a concluding message with the word "TERMINATE". Otherwise, repeat the above process with a more optimal solution if it exists.
"""

assistant = autogen.AssistantAgent(
    name="assistant",
    llm_config=llm_config,
    system_message=SYSTEM_MESSAGE
)

# create a UserProxyAgent instance named "user_proxy"
user_proxy = autogen.UserProxyAgent(
    name="user_proxy",
    human_input_mode="NEVER",
    max_consecutive_auto_reply=4,
    is_termination_msg=lambda x: x.get("content", "").rstrip().endswith("TERMINATE"),
    code_execution_config={
        "work_dir": "coding",
        "use_docker": False,
    },
)

# Use DiskCache as cache
with Cache.disk(cache_seed=7) as cache:
  # the assistant receives a message from the user_proxy, which contains the task description
  chat_res = user_proxy.initiate_chat(
      assistant,
      message="""Solve the following leetcode problem and also comment on it's time and space complexity:nn""" + LEETCODE_QUESTION
)

```

