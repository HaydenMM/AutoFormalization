# Autoformalization Pass@K Evaluation

This repository contains the experimental setup and evaluation scripts for **Pass@K robustness testing** of **Autoformalization pipelines** using the **Isabelle theorem prover**, **MiniF2F dataset**, and **GPT-4o-mini** as the semantic parser.

---

## 🧩 Overview

We evaluate the **robustness and round-trip fidelity** of natural-language (NL) to formal logic translation, and vice versa.  
Our approach measures **consistency degradation** across chained translations and quantifies performance via **Pass@K** metrics.

---

## 🧠 Experimental Design

### 1. Ground-Truth NL → 10 Formal
We begin with **ground-truth natural language (NL)** statements and prompt **GPT-4o-mini** to produce **10 candidate formal statements** in Isabelle syntax.  
These are checked for semantic equivalence and theorem validity using **Isabelle**.

### 2. Ground-Truth Formal → 10 NL → 10 Formal
We then start from the **ground-truth formal statements**, generate **10 NL paraphrases**, and re-formalize each into **10 new formal candidates**.  
This tests **autoformalization robustness under linguistic perturbation** — i.e., whether meaning is preserved across translation cycles.

---

## 📊 Metrics

- **Pass@K** (for K ∈ {1, 5, 10}):  
  The fraction of problems solved correctly within the top-K generated candidates.
- **Round-Trip Consistency:**  
  Measures semantic alignment between original and regenerated formals.
- **Equivalence Checking:**  
  Verified through Isabelle proof obligations.

---

## 🧪 Tools & Dependencies

- [**Isabelle**](https://isabelle.in.tum.de/) — theorem prover used for formal verification and equivalence checking.
- **GPT-4o-mini** — lightweight LLM used for semantic parsing and translation generation.
- **MiniF2F** — benchmark dataset for formal mathematics (Lean/Isabelle translations).

Ensure Isabelle and its dependencies are installed and configured in your environment before running the evaluation scripts.

---

## ⚙️ Pipeline Summary

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ Ground Truth NL                                                             │
│    │                                                                        │
│    └──> GPT-4o-mini → 10 Formal (Isabelle)                                  │
│                     ↓                                                      │
│             Pass@K Evaluation                                               │
│                                                                             │
│ Ground Truth Formal                                                         │
│    │                                                                        │
│    └──> GPT-4o-mini → 10 NL → 10 Formal                                     │
│                     ↓                                                      │
│             Pass@K + Round-Trip Consistency                                 │
└─────────────────────────────────────────────────────────────────────────────┘

