from math import floor, log

# --------- normalized log curves ---------

def frac_closed_norm_log(r: float, alpha: float, r_max: float) -> float:
    """Profit: r -> fraction closed in [0,1]."""
    if r <= 0:
        return 0.0
    r = min(r, r_max)
    return log(1.0 + alpha * r) / log(1.0 + alpha * r_max)


def frac_cut_norm_log(neg_r: float, beta: float, r_stop: float) -> float:
    """Loss: neg_r (>=0) -> fraction cut in [0,1]."""
    if neg_r <= 0:
        return 0.0
    neg_r = min(neg_r, r_stop)
    return log(1.0 + beta * neg_r) / log(1.0 + beta * r_stop)