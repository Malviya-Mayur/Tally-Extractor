# Tally Extractor

Tally Extractor is a powerful, local web application designed to smoothly extract and process transactional data from Tally ERP. Operating completely offline, it can interface directly with your active Tally software or parse previously extracted Tally XML files. The resulting data is intelligently flattened into a `.xlsx` file designed for analysis and reporting.

![Tally Pipeline UI UI Screenshot](https://raw.githubusercontent.com/Malviya-Mayur/Tally-Extractor/main/tally_web/frontend/assets/ui_screenshot.png) *(Note: Replace UI screenshot link with actual Github asset link if added)*

## Features

- **Live Tally API Connection**: Connects to Tally running locally (e.g., port 9000) and extracts data dynamically for any date range.
- **Offline XML Upload**: Process an existing XML extraction without needing Tally actively running. Just drag, drop, and extract.
- **Comprehensive Data Extraction**: Flattens nested Tally transactional structures (Vouchers, Ledgers, Inventory) into a unified dataset.
- **Consolidated Excel Output**: Automatically generates a dual-sheet `.xlsx` file containing an organized "Data" sheet and an "Extraction Log" sheet.
- **Star Schema Support**: Export associated dimension tables like `STOCKITEM`, `LEDGER`, `GROUP`, etc., as separate `.csv` files alongside your Excel export.
- **Web Interface**: A sleek, dark-mode GUI with progress tracking, logging, and an easy-to-use form.

---

## 🚀 Installation & Setup

1. **Clone the Repository**
   ```bash
   git clone https://github.com/Malviya-Mayur/Tally-Extractor.git
   cd "Tally Extractor"
   ```

2. **Load the TDL File in Tally**
   In order for the live extraction to work, you must load the specific custom report into Tally.
   - Open Tally.
   - Navigate to `F1: Help` > `TDLs & Add-Ons` > `Manage Local TDLs` (or press `F4`).
   - Set `Load TDL files on startup` to `Yes`.
   - Add the absolute path to `APIRawVouchers.tdl` (found in the root of this repository).
   - Ensure the TDL is loaded without errors.

3. **Install Python Dependencies**
   Ensure you have Python 3.10+ installed. It is recommended to create a virtual environment first.

   **For Windows:**
   ```powershell
   cd tally_web
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```

   **For Linux / macOS:**
   ```bash
   cd tally_web
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

---

## 💻 Usage

1. **Start the Web Application**
   Depending on your operating system, run the app using the available scripts or commands from the `tally_web` directory:

   **For Windows:**
   ```powershell
   # If you are in the active venv
   python backend\app.py
   ```

   **For Linux / macOS:**
   ```bash
   # You can run the provided bash script
   bash start.sh
   # Or run manually in the active venv:
   # python3 backend/app.py
   ```

2. **Access the Interface**
   Open your browser and navigate to:
   ```
   http://127.0.0.1:8080/
   ```

3. **Running an Extraction**
   - Choose your **Data Source**:
     - **🔌 Live Tally API**: Enter the specific date range you want to extract. Make sure Tally is open and running the selected company. Ensure the port matches Tally's configured client/server port (default: 9000).
     - **📂 Upload XML File**: If you already have an exported XML from the `APIRawVouchers` report, you can drag and drop it here to parse it.
   - Configure **Additional Exports** (optional) if you need specific dimension tables mapped.
   - Click **Start Extraction**.

4. **Retrieving Your Data**
   Once parsing finishes, a download link to an Excel file (`.xlsx`) will appear. This file contains your flat transactional data along with a background log tracking the extraction details. By default, files are also saved back into the `tally_web/tally_out` folder.

---

## Architecture Context

* `Tally_Pipeline.py`: The core python script that handles HTTP requests to Tally and parses the returned XML tree into a flat schema.
* `APIRawVouchers.tdl`: The Tally Definition Language configuration to expose the required data report.
* `tally_web/`: The FastAPI + HTML/JS web server wrapping the pipeline functionality into a clean user interface.
