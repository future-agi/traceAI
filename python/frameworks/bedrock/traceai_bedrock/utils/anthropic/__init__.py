def _extract_image_data(messages):
    try:
        input_images = []
        filtered_messages = []
        eval_input = []

        if isinstance(messages, list):
            for message in messages:
                filtered_content = []
                content = message.get("content", [])

                # Handle both string and list content
                if isinstance(content, str):
                    filtered_messages.append(message)
                    eval_input.append(content)
                    continue

                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            if "image" in item:
                                source = item["image"].get("source", {})
                                if "bytes" in source:
                                    data = source["bytes"]
                                    if data:
                                        input_images.append(data)
                            else:
                                # Keep non-image content
                                filtered_content.append(item)
                                if "text" in item and item.get("text"):
                                    eval_input.append(str(item.get("text")))

                # Create new message with filtered content
                if filtered_content:
                    filtered_message = message.copy()
                    filtered_message["content"] = filtered_content
                    filtered_messages.append(filtered_message)

        return {
            "input_images": input_images if input_images else None,
            "filtered_messages": filtered_messages if filtered_messages else None,
            "eval_input": " | ".join(eval_input),
        }
    except Exception as e:
        print(f"Error in _extract_image_data: {e}")
        return {"images": None, "filtered_messages": messages}
