# Load a CSV into SQLite

Turn a CSV file into a queryable SQLite table using only the Python standard library.

## 1. Data flow

A CSV goes in, a `data.db` table comes out, and one query checks the result. Name the table the loader creates and the file it reads.

## 2. Write the CSV

Create `data.csv` with a header row `name,qty` and three data rows.

## 3. Write the loader

Create `load.py`. It creates the `items` table in `data.db` and inserts each CSV row.

## 4. Load and count

Run `python3 load.py`, then count the rows in `items`. The count should be 3.

## 5. Query

List the names in order. You should see apple, pear, and plum.

## 6. Backup

Note where you keep a copy of `data.db`.
