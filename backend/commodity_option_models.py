"""Chapter 12 analytics for snapshots of oil option premiums."""

from math import erfc, exp, isfinite, log, pi, sqrt


def normal_cdf(x):
    return 0.5 * erfc(-x / sqrt(2))


def option_premium(forward, strike, deviation, side, black=False):
    """Undiscounted option value, with total (rather than annualized) deviation."""
    direction = 1 if side == 'call' else -1
    distance = direction * (forward - strike)
    if deviation <= 0:
        return max(distance, 0)
    if black:
        d1 = log(forward / strike) / deviation + deviation / 2
        d2 = d1 - deviation
        return direction * (forward * normal_cdf(direction * d1) - strike * normal_cdf(direction * d2))
    z = distance / deviation
    return distance * normal_cdf(z) + deviation * exp(-z * z / 2) / sqrt(2 * pi)


def implied_volatility(premium, forward, strike, years, side, black=False):
    if years <= 0 or premium <= max(0, (forward - strike) * (1 if side == 'call' else -1)) + 1e-9:
        return None
    if black and (min(forward, strike) <= 0 or premium >= (forward if side == 'call' else strike)):
        return None
    low, high = 0, 0.5 if black else max(1, premium)
    while option_premium(forward, strike, high, side, black) < premium:
        high *= 2
    for _ in range(48):
        mid = (low + high) / 2
        if option_premium(forward, strike, mid, side, black) < premium:
            low = mid
        else:
            high = mid
    return (low + high) / (2 * sqrt(years))


def atm_interpolation(points, forward, field):
    valid = [point for point in points if point[field] is not None]
    lower = next((point for point in reversed(valid) if point['strike'] <= forward), None)
    upper = next((point for point in valid if point['strike'] >= forward), None)
    if not lower or not upper:
        return None
    distance = upper['strike'] - lower['strike']
    if distance == 0:
        return lower[field]
    if max(forward - lower['strike'], upper['strike'] - forward) > max(1.5, abs(forward) * 0.08):
        return None
    weight = (forward - lower['strike']) / distance
    return (1 - weight) * lower[field] + weight * upper[field]


def quadratic_fit(points, forward, scale):
    """Fit the normal IV in scaled strike coordinates for numerical stability."""
    sample = [((point['strike'] - forward) / scale, point['normal_iv']) for point in points
              if abs(point['strike'] - forward) <= 2.5 * scale]
    if len(sample) < 5 or sum(x < 0 for x, _ in sample) < 2 or sum(x > 0 for x, _ in sample) < 2:
        return None
    weights = [exp(-x * x / 2) for x, _ in sample]
    moments = [sum(w * x ** degree for (x, _), w in zip(sample, weights)) for degree in range(5)]
    matrix = [
        [*moments[i:i + 3], sum(w * y * x ** i for (x, y), w in zip(sample, weights))]
        for i in range(3)
    ]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda row: abs(matrix[row][col]))
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        divisor = matrix[col][col]
        if abs(divisor) < 1e-12:
            return None
        matrix[col] = [value / divisor for value in matrix[col]]
        for row in range(3):
            if row != col:
                factor = matrix[row][col]
                matrix[row] = [a - factor * b for a, b in zip(matrix[row], matrix[col])]
    a, b, c = [row[-1] for row in matrix]
    return {
        'atm': a, 'slope': b / scale, 'curvature': c / scale ** 2, 'count': len(sample),
        'strike_min': forward + min(x for x, _ in sample) * scale,
        'strike_max': forward + max(x for x, _ in sample) * scale,
    }


def increasing_slopes(slopes, intervals):
    """Weighted isotonic projection onto no-static-arbitrage vertical spreads."""
    blocks = []
    for slope, width in zip(slopes, intervals):
        blocks.append([slope * width, width, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            right = blocks.pop()
            left = blocks.pop()
            blocks.append([a + b for a, b in zip(left, right)])
    return [max(-1, min(0, total / width)) for total, width, count in blocks for _ in range(count)]


def risk_neutral_distribution(points):
    if len(points) < 5:
        return None
    intervals = [right['strike'] - left['strike'] for left, right in zip(points, points[1:])]
    slopes = [(right['call_equivalent'] - left['call_equivalent']) / width
              for left, right, width in zip(points, points[1:], intervals)]
    adjusted = increasing_slopes(slopes, intervals)
    butterflies = []
    for i in range(1, len(points) - 1):
        width = (intervals[i - 1] + intervals[i]) / 2
        mass = max(0, adjusted[i] - adjusted[i - 1])
        butterflies.append({
            'strike': points[i]['strike'], 'density': mass / width, 'mass': mass,
            'raw_density': (slopes[i] - slopes[i - 1]) / width,
        })
    return {
        'butterflies': butterflies,
        'cdf': [{'strike': point['strike'], 'probability': 1 + slope}
                for point, slope in zip(points, [*adjusted, adjusted[-1]])],
        'covered_mass': adjusted[-1] - adjusted[0],
        'violating_verticals': sum(slope < -1 - 1e-7 or slope > 1e-7 for slope in slopes),
        'violating_butterflies': sum(right < left - 1e-7 for left, right in zip(slopes, slopes[1:])),
        'adjusted_intervals': sum(abs(raw - fitted) > 1e-7 for raw, fitted in zip(slopes, adjusted)),
    }


def calibrate_expiry(expiry, contract, forward, years, quotes, rate):
    discount = exp(-rate * years)
    points = []
    for strike, sides in sorted(quotes.items()):
        side = 'put' if strike < forward else 'call'
        quote = sides.get(side)
        if quote is None and strike == forward:
            side, quote = 'put', sides.get('put')
        if not quote or not isfinite(quote['price']) or quote['price'] <= 0:
            continue
        undiscounted = quote['price'] / discount
        normal = implied_volatility(undiscounted, forward, strike, years, side)
        if normal is None:
            continue
        points.append({
            'strike': strike, 'side': side, 'premium': quote['price'], 'bid': quote.get('bid'),
            'ask': quote.get('ask'), 'quote_time': quote.get('quote_time'),
            'call_equivalent': undiscounted + (forward - strike if side == 'put' else 0),
            'normal_iv': normal, 'black_iv': implied_volatility(undiscounted, forward, strike, years, side, black=True),
            'fitted_iv': None, 'local_vol': None, 'model_premium': None, 'residual': None,
        })
    atm = atm_interpolation(points, forward, 'normal_iv')
    fit = quadratic_fit(points, forward, atm * sqrt(years)) if atm else None
    errors = []
    if fit:
        for point in points:
            if not fit['strike_min'] - 1e-8 <= point['strike'] <= fit['strike_max'] + 1e-8:
                continue
            x = point['strike'] - forward
            fitted = fit['atm'] + fit['slope'] * x + fit['curvature'] * x * x
            local = fit['atm'] + 2 * fit['slope'] * x + 3 * fit['curvature'] * x * x
            if local > 0:
                point['local_vol'] = local
            if fitted > 0:
                point['fitted_iv'] = fitted
                model_price = discount * option_premium(forward, point['strike'], fitted * sqrt(years), point['side'])
                point['model_premium'] = model_price
                point['residual'] = point['premium'] - model_price
                errors.append(point['residual'] ** 2)
        fit['rmse'] = sqrt(sum(errors) / len(errors)) if errors else None
    return {
        'expiry': expiry, 'contract': contract, 'forward': forward, 'days': years * 365,
        'years': years, 'atm_normal_iv': atm, 'atm_black_iv': atm_interpolation(points, forward, 'black_iv'),
        'points': points, 'fit': fit, 'distribution': risk_neutral_distribution(points),
    }


def bootstrap_term(slices, field):
    previous_years = previous_variance = 0
    result = []
    for expiry in sorted(slices, key=lambda row: row['years']):
        years, implied = expiry['years'], expiry[field]
        if implied is None or years <= previous_years:
            continue
        variance = years * implied ** 2
        change = variance - previous_variance
        forward_variance = change / (years - previous_years)
        result.append({
            'expiry': expiry['expiry'], 'contract': expiry['contract'], 'days': expiry['days'],
            'interval_start_days': previous_years * 365, 'atm_iv': implied, 'total_variance': variance,
            'local_variance': forward_variance, 'local_vol': sqrt(max(forward_variance, 0)) if change >= -1e-10 else None,
            'negative_variance': change < -1e-10,
        })
        previous_years, previous_variance = years, variance
    return result
