"""Optional display metadata, kept separate from AirPlay identity and playback."""


def _text(value):
    if not isinstance(value, str):
        return ''
    return ''.join(c for c in value[:256] if c.isprintable()).strip()


def receiver_metadata(receiver, *services):
    model = raw_model = ''
    try:
        info = receiver.device_info
        model = _text(getattr(info, 'model_str', ''))
        raw_model = _text(getattr(info, 'raw_model', ''))
    except Exception:
        # Missing or malformed optional metadata must not remove a usable receiver.
        pass
    for service in services:
        if raw_model:
            break
        try:
            properties = service.properties
            raw_model = _text(properties.get('model')) or _text(properties.get('am'))
        except Exception:
            continue
    return {key: value for key, value in {'model': model or raw_model, 'modelIdentifier': raw_model}.items() if value}
