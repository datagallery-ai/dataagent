1. `neutralization_experiments.id` 和 `neutralization_ic50_fits.id` 都是子类→父类 `experiments.id` 的继承身份 PK-FK（两者各自是一行 experiment 子类），**不是**"实验→它的拟合结果"的关系。**禁止**用 `neutralization_ic50_fits.id = neutralization_experiments.id` 关联这两张表，这样做会得到空结果或错位结果。
2. 关联 `neutralization_experiments` ↔ `neutralization_ic50_fits` 的**唯一正确路径**是经 `neutralization_data` 中转：
   `neutralization_experiments.result_id = neutralization_ic50_fits.input_data_id`
   （两者都引用 `neutralization_data.id`，可直接相等，无需显式 JOIN `neutralization_data` 表）。
3. `neutralization_ic50_fits.output_data_id → neutralization_ic50_fit_data.id` 才是 fit→拟合输出数据的关系；`ic50` 和 `fit_success` 列在 `neutralization_ic50_fit_data` 表上，不在 `neutralization_ic50_fits` 上。
4. 抗体名称存放在 `proteins.name`（如 'BD-368'、'BD55-1111'），通过 `antibodies.id = proteins.id` 关联到 `antibodies`；不要用 `antibodies.id` 当名称筛选。
5. 假病毒名称存放在 `pseudoviruses.name`，通过 `pseudovirus_samples.pseudovirus_id = pseudoviruses.id` 关联。
6. "有效中和（IC50 < 阈值）"语义需同时满足 `neutralization_ic50_fit_data.fit_success = 1 AND neutralization_ic50_fit_data.ic50 < 阈值`。
7. 当 schema 中某张表的 `id` 列同时是 PK 且 FK 指向另一张父表的 `id` 时，该 FK 表示"子类身份继承"而非"业务关系"，不得用作 JOIN 键；必须改用该表中指向业务实体的 FK 列（通常命名为 `*_id`、`result_id`、`input_data_id`、`output_data_id` 等）。
