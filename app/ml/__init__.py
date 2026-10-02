"""Machine-learning issue classifier (TF-IDF + logistic regression).

* :mod:`app.ml.preprocess` — text cleaning shared by training and inference.
* :mod:`app.ml.model` — the classifier, its explanations, and safe (pickle-free)
  save / load.
* :mod:`app.ml.train` — fitting, hyper-parameter search and metrics.
* :mod:`app.ml.service` — runtime decision logic with rule-based fallback.
"""
