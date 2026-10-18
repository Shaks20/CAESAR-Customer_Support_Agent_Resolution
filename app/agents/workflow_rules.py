def classify_complaint_rules(text: str) -> str:
    value = text.lower()
    # Safety precedence: damage claims must enter the evidence/inspection workflow even
    # when the customer also asks for a refund or replacement.
    if any(word in value for word in (
        "damaged", "damage", "broken", "cracked", "defective", "faulty", "malfunction",
        "not working", "doesn't work", "does not work", "dead on arrival", "shattered", "doa",
    )):
        return "damaged_item"
    if any(word in value for word in ("refund", "money back", "reimburse", "returned my")):
        return "refund"
    if any(word in value for word in ("where is", "shipping", "delivery", "tracking", "order status")):
        return "order_status"
    return "other"
