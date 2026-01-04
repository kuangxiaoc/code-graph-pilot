def validate_input(data):
    print("Validating...")
    return True

def save_to_db(data):
    print("Saving...")

def process_data(data):
    if validate_input(data):
        save_to_db(data)
    return "Done"

def main():
    data = "test"
    process_data(data)