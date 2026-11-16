"""Model/search projections keep stored attachment bytes out of text context."""


def text_source_content(value):
    if isinstance(value, dict):
        return {
            key: text_source_content(item) for key, item in value.items() if key != "content_base64"
        }
    if isinstance(value, list):
        return [text_source_content(item) for item in value]
    return value


def model_source_content(source):
    return text_source_content(source.content)
