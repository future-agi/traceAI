from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from llama_index.core.multi_modal_llms.generic_utils import load_image_urls
from llama_index.multi_modal_llms.openai import OpenAIMultiModal
from llama_index.multi_modal_llms.openai.utils import (
    generate_openai_multi_modal_chat_message,
)
from traceai_llamaindex import LlamaIndexInstrumentor

# Configure trace provider with custom evaluation tags
eval_tags = [
    EvalTag(
        eval_name=EvalName.DETERMINISTIC_EVALS,
        value=EvalSpanKind.TOOL,
        type=EvalTagType.OBSERVATION_SPAN,
        config={
            "multi_choice": False,
            "choices": ["Yes", "No"],
            "rule_prompt": "Evaluate if the response is correct",
        },
        custom_eval_name="<custom_eval_name>",
    )
]

# Configure trace provider with custom evaluation tags
trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    eval_tags=eval_tags,
    project_name="FUTURE_AGI",
    project_version_name="v1",
)

# Initialize the Llama Index instrumentor
LlamaIndexInstrumentor().instrument(tracer_provider=trace_provider)


IMAGE_URLS = [
    # "https://www.visualcapitalist.com/wp-content/uploads/2023/10/US_Mortgage_Rate_Surge-Sept-11-1.jpg",
    # "https://www.sportsnet.ca/wp-content/uploads/2023/11/CP1688996471-1040x572.jpg",
    "https://res.cloudinary.com/hello-tickets/image/upload/c_limit,f_auto,q_auto,w_1920/v1640835927/o3pfl41q7m5bj8jardk0.jpg",  # noqa: E501
    # "https://www.cleverfiles.com/howto/wp-content/uploads/2018/03/minion.jpg",
]

if __name__ == "__main__":
    image_documents = load_image_urls(IMAGE_URLS)

    openai_mm_llm = OpenAIMultiModal(
        model="gpt-4o",
    )

    chat_msg_1 = generate_openai_multi_modal_chat_message(
        prompt="Describe the images as an alternative text",
        role="user",
        image_documents=image_documents,
    )

    chat_msg_2 = generate_openai_multi_modal_chat_message(
        prompt="The image is a graph showing the surge in US mortgage rates. It is a visual representation of data, with a title at the top and labels for the x and y-axes. Unfortunately, without seeing the image, I cannot provide specific details about the data or the exact design of the graph.",  # noqa: E501
        role="assistant",
    )

    chat_msg_3 = generate_openai_multi_modal_chat_message(
        prompt="can I know more?",
        role="user",
    )
    response_gen = openai_mm_llm.stream_chat(
        # prompt="Describe the images as an alternative text",
        messages=[
            chat_msg_1,
            chat_msg_2,
            chat_msg_3,
        ],
        stream_options={
            "include_usage": True,
        },
    )
    for response in response_gen:
        print(response.delta, end="")
