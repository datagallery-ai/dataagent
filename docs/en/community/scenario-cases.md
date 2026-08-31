# Scenario cases

Scenario cases help users understand how DataFoundry handles realistic data-agent tasks across schema inspection, read-only SQL, trace evidence, and final operating decisions.

## What makes a good case

A strong case should be concrete enough to reproduce and polished enough to show externally:

- A recognizable business or operational scenario.
- A small demo-safe dataset or schema outline.
- A clear analysis path, including the questions a user would ask.
- Expected findings that can be validated from the data.
- Notes about governance, privacy, access control, or production boundaries.

The [DTC growth operating review](../examples/dtc-growth-demo.md) is the current reference case. It combines orders, ad spend, products, support tickets, and targets into one traceable operating review.

## How to propose one

Open a [GitHub issue](https://github.com/datagallery-lab/datafoundry/issues) with:

- Case title and target audience.
- Business question or workflow goal.
- Tables, files, APIs, or external systems involved.
- Expected analysis steps and representative outputs.
- What must be anonymized, mocked, or excluded.

Cases should not require private data access. If the real scenario is sensitive, describe the workflow shape and provide a sanitized dataset or synthetic equivalent.
