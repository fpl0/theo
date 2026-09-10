CREATE TABLE effects(
    change_id TEXT NOT NULL REFERENCES changes(id),
    name TEXT NOT NULL,
    request TEXT NOT NULL CHECK(json_valid(request)),
    status TEXT NOT NULL CHECK(status IN ('intent','succeeded','uncertain')),
    receipt TEXT CHECK(receipt IS NULL OR json_valid(receipt)),
    updated_at REAL NOT NULL,
    PRIMARY KEY(change_id,name)
);
CREATE TABLE signals(
    change_id TEXT NOT NULL REFERENCES changes(id),
    name TEXT NOT NULL,
    body TEXT NOT NULL CHECK(json_valid(body)),
    PRIMARY KEY(change_id,name)
);
