# Tally Extractor

Tally Extractor is a powerful, local web application designed to smoothly extract and process transactional data from Tally ERP/Prime. Operating completely offline, it can interface directly with your active Tally software or parse previously extracted Tally XML files. The resulting data is intelligently flattened into a `.xlsx` file designed for analysis and reporting.

![Tally Pipeline UI UI Screenshot](https://raw.githubusercontent.com/Malviya-Mayur/Tally-Extractor/main/tally_web/frontend/assets/ui_screenshot.png)

## Features

- **Live Tally API Connection**: Connects to Tally running locally (e.g., port 9000) and extracts data dynamically for any date range in chunked requests to save memory.
- **Offline XML Upload**: Process an existing XML extraction without needing Tally actively running. Just drag, drop, and extract.
- **Comprehensive Data Extraction**: Flattens nested Tally transactional structures (Vouchers, Ledgers, Inventory) into a unified dataset using a blazing-fast single XML walk.
- **Consolidated Excel Output**: Automatically generates a dual-sheet `.xlsx` file containing an organized "Data" sheet and an "Extraction Log" sheet.
- **Star Schema Support**: Export associated dimension tables like `STOCKITEM`, `LEDGER`, `GROUP`, etc., as separate `.csv` files alongside your Excel export.
- **Web Interface**: A sleek, dark-mode GUI with progress tracking, logging, and an easy-to-use form.

---

## 🚀 Installation

We offer multiple ways to install Tally Extractor depending on your operating system and internet connectivity.

### Option A: Standalone Installers (Requires Internet)
These scripts will automatically download the codebase from GitHub, set up a virtual environment, and install all dependencies. 

**For Windows:**
1. Download `install_windows.bat`.
2. Double-click it. It will install the application to `%USERPROFILE%\Tally-Extractor`, create a Desktop shortcut, and add a Start Menu entry.

**For Arch Linux:**
1. Download `install_arch.sh`.
2. Run it via terminal: `chmod +x install_arch.sh && ./install_arch.sh`.
3. It will install to `~/.local/share/tally-extractor`, create a `.desktop` app menu entry, and set up a `tallyextractor` CLI command.

### Option B: Fully Offline Installers (No Internet Required)
If the target machine does not have internet access, you can generate fully standalone offline bundles.

1. On a machine **with** internet, run:
   ```bash
   python3 tools/create_offline_dist.py
   ```
2. This generates two self-contained archives in the `dist/` folder containing the source code and all compiled pip wheels:
   - `tally-extractor-windows.zip`
   - `tally-extractor-arch-linux.tar.gz`
3. Copy the archive to the offline machine, extract it, and run the `install.bat` or `install.sh` inside. It will install without touching the network.

### Option C: Arch Linux PKGBUILD (AUR-style)
For Arch users who prefer standard packaging workflows:
```bash
git clone https://github.com/Malviya-Mayur/Tally-Extractor.git
cd Tally-Extractor
makepkg -si
```

---

## 💻 Usage Guide

### 1. Configure Tally Prime
Before your first extraction, you must load the specific custom report into Tally so the tool can request data.

1. Open Tally Prime and load your company.
2. Navigate to **F1: Help > TDLs & Add-Ons > Manage Local TDLs** (or press `F4`).
3. Set `Load TDL files on startup` to `Yes`.
4. Add the absolute path to `API_Extractor.txt` (found in the root directory).
5. Enable Tally's HTTP Server: **F1: Help > Settings > Connectivity > Enable TallyPrime Server**. Set the Port to `9000`.

### 2. Start the Application
Depending on how you installed it:
- **Windows**: Double-click the **Tally Extractor** shortcut on your Desktop or Start Menu.
- **Arch Linux**: Run `tallyextractor` from your terminal or launch it from your application menu. 

### 3. Extracting Data
1. The web interface will open automatically at `http://127.0.0.1:8888`.
2. Under **Data Source**, choose **Live Tally API**.
3. Set the desired **Date Range** and ensure the **Tally Server Port** matches (default: `9000`).
4. Click **Start Extraction**. You can monitor the progress and live logs directly in the browser terminal.

---

## 📊 Sample Output Format

Once parsing finishes, a download link to an Excel file (`.xlsx`) will appear (for example, `Mayur_extraction_Apr-25-Mar-26_20260610_210333.xlsx`).

The generated workbook contains two sheets:

1. **Data Sheet**: A flat, granular table representing your transactions. Every row represents a specific ledger/inventory allocation line within a voucher. Standard columns include:
   - `posting_date`, `voucher_type`, `voucher_number`
   - `ledger_name`, `party_ledger_name`, `amount_absolute`, `debit_credit_flag`
   - Master details like `gstin`, `pan`, `company_state`
   - Inventory specifics like `stock_item_name`, `godown_name`, `quantity`, `rate`
   - *Note: This dataset is extremely comprehensive and spans over 70+ columns designed directly for pivot tables, BI ingestion (PowerBI/Tableau), and advanced reporting.*

2. **Extraction Log Sheet**: Contains the complete background log of the extraction session, including timestamps, chunked processing times, and any warnings encountered during data retrieval.
