


from datetime import datetime
import uuid

def convert_for_json(data_list):
    """
    Recursively convert UUIDs and datetime objects in a list of dicts to JSON-serializable types
    """
    for item in data_list:
        for k, v in item.items():
            if isinstance(v, uuid.UUID):
                item[k] = str(v)
            elif isinstance(v, datetime):
                item[k] = v.isoformat()
    return data_list