import re
from typing import Tuple, List, Optional
from app.models.offer import ProductAnalysis

def validate_offer_fast(
    title: str,
    price: float,
    analysis: Optional[ProductAnalysis] = None
) -> Tuple[bool, str]:
    """
    Lightning-fast heuristic check to filter out accessories, toys,
    prebuilts, cables, and wrong hardware tiers.
    """
    if price <= 0:
        return False, "Price <= 0"

    t_lower = (title or "").lower()

    # Universal spam / non-hardware keywords
    spam_terms = [
        "keychain", "poster", "t-shirt", "mug", "decal", "sticker",
        "dummy model", "display model", "replica", "toy", "box only",
        "empty box", "paper edition", "picture only"
    ]
    for term in spam_terms:
        if term in t_lower:
            return False, f"Matches spam keyword: '{term}'"

    # Category / Model specific validation if analysis provided
    if analysis:
        cat_upper = (analysis.category or "").upper()
        raw_lower = (analysis.canonical_name or "").lower()
        model_lower = (analysis.model or "").lower()

        # Category accessory filtering
        if cat_upper in ["CPU", "PROCESSOR"]:
            cpu_acc = [
                "cpu cooler", "air cooler", "liquid cooler", "aio cooler", "water cooler",
                "cooler master", "thermalright", "noctua", "deepcool", "thermal paste",
                "contact frame", "mounting bracket", "retention bracket", "cpu fan"
            ]
            for acc in cpu_acc:
                if re.search(r'\b' + re.escape(acc) + r'\b', t_lower):
                    if acc not in raw_lower and acc not in model_lower:
                        return False, f"Accessory item: '{acc}'"
        elif cat_upper in ["GPU", "GRAPHICS CARD"]:
            gpu_acc = [
                "gpu bracket", "support bracket", "riser cable", "vertical mount",
                "backplate", "water block", "cooling fan replacement"
            ]
            for acc in gpu_acc:
                if re.search(r'\b' + re.escape(acc) + r'\b', t_lower):
                    if acc not in raw_lower and acc not in model_lower:
                        return False, f"Accessory item: '{acc}'"

        # Negative keywords from product analysis
        negatives = analysis.specs.get("negative_keywords", [])
        for neg in negatives:
            if neg.lower() in t_lower:
                return False, f"Matches negative keyword: '{neg}'"

        # Numeric model token matching (e.g. 4770, 10700, 6700, 7800, 4070)
        nums = re.findall(r'(?<!\d)(\d{3,5})(?!\d)', analysis.model)
        for num in nums:
            if not re.search(r'(?<!\d)' + num + r'(?!\d)', t_lower):
                return False, f"Missing model number {num}"

        # Critical variant suffix matching (e.g. K, KF, F, X, X3D, Ti, XT, XTX, Super)
        critical_suffixes = ["x3d", "xtx", "kf", "ti", "xt", "super", "k", "f", "x"]
        found_suffix = None
        for s in critical_suffixes:
            if re.search(r'(?:\b|(?<=\d))' + re.escape(s) + r'\b', model_lower):
                found_suffix = s
                break

        if found_suffix and nums:
            if not re.search(r'(?:\b|(?<=\d))' + re.escape(found_suffix) + r'\b', t_lower):
                return False, f"Missing requested variant '{found_suffix.upper()}'"

        # Relaxed price floor check (preserves genuine cheap used/refurb deals, rejects $2 stickers/cables)
        min_price = analysis.target_price or analysis.specs.get("min_price")
        if min_price and float(min_price) > 0:
            floor = max(8.0, float(min_price) * 0.15)
            if price < floor:
                return False, f"Price ${price:.2f} is below minimum hardware floor (${floor:.2f})"

            # Realistic ceiling check (rejects prebuilts, motherboard bundles, and recommended carousel leaks)
            mult = 3.0 if cat_upper in ["CPU", "PROCESSOR"] else 3.5
            ceiling = max(float(min_price) * mult, float(min_price) + 250.0)
            if price > ceiling:
                return False, f"Price ${price:.2f} exceeds realistic ceiling (${ceiling:.2f}) for standalone {analysis.model}"

    return True, "Valid offer"
