"""Damped, numerically stable Platt fit for retrained TD candidates."""
import numpy as np


def stable_platt(p, y):
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1-1e-6)
    y = np.asarray(y, dtype=float)
    x = np.column_stack([np.ones(len(p)), np.log(p/(1-p))])
    rate = np.clip(y.mean(), 1e-6, 1-1e-6)
    coef = np.array([np.log(rate/(1-rate)), 0.0])
    def objective(b):
        z = x@b
        return float(np.sum(np.logaddexp(0, z)-y*z) + 5e-7*(b@b))
    for _ in range(100):
        prob = 1/(1+np.exp(-np.clip(x@coef, -35, 35)))
        gradient = x.T@(prob-y) + 1e-6*coef
        hessian = x.T@(x*(prob*(1-prob))[:, None]) + np.eye(2)*1e-6
        step = np.linalg.solve(hessian, gradient)
        scale, previous = 1., objective(coef)
        while scale > 1e-8 and objective(coef-scale*step) > previous:
            scale *= .5
        coef -= scale*step
        if np.max(np.abs(scale*step)) < 1e-7:
            break
    if not np.isfinite(coef).all():
        raise ValueError('Nonfinite calibration parameters')
    return float(coef[0]), float(coef[1])
