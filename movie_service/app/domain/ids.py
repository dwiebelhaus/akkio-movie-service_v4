# Ids are Postgres bigints. A larger value would be sent as `numeric`, and comparing a bigint
# column with a numeric can't use the primary key index: a full table scan per request.
MAX_ID = 2**63 - 1
