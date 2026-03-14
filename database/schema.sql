-- Bank Statement Extractor — Template Storage Schema
-- Run this in phpMyAdmin or MySQL CLI to set up the database.

CREATE DATABASE IF NOT EXISTS bank_extractor
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE bank_extractor;

-- ─── Templates ───────────────────────────────────────────────────────────────
-- Each row represents a unique PDF layout (bank statement format).
-- When a PDF is successfully extracted, its layout fingerprint is saved here
-- so future PDFs with the same layout can reuse the proven column mapping.

CREATE TABLE IF NOT EXISTS templates (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    fingerprint     VARCHAR(64)   NOT NULL UNIQUE,     -- SHA-256 of normalized layout
    bank_name       VARCHAR(255)  DEFAULT NULL,         -- User-assigned label (optional)
    column_count    INT           NOT NULL,             -- Number of table columns
    col_map         JSON          NOT NULL,             -- {"date":0, "description":1, "debit":2, ...}
    header_texts    JSON          NOT NULL,             -- ["Date", "Description", "Debit", ...]
    header_x_positions JSON       DEFAULT NULL,         -- [45.2, 150.8, 320.1, ...] (optional)
    strategy        VARCHAR(20)   NOT NULL,             -- "pymupdf" | "pdfplumber" | "words" | "text"
    header_row_count INT          NOT NULL DEFAULT 1,   -- Rows to skip before data
    page_width      FLOAT         DEFAULT NULL,         -- PDF page width in points
    page_height     FLOAT         DEFAULT NULL,         -- PDF page height in points
    date_format     VARCHAR(30)   DEFAULT NULL,         -- e.g. "DD/MM/YYYY" (detected pattern)
    success_count   INT           NOT NULL DEFAULT 1,   -- Times this template was used successfully
    created_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_fingerprint (fingerprint)
) ENGINE=InnoDB;

-- ─── Column Aliases ──────────────────────────────────────────────────────────
-- Maps header text → role for headers not recognized by the built-in keyword sets.
-- Enriches classify_column_header() with learned mappings.

CREATE TABLE IF NOT EXISTS column_aliases (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    template_id     INT           NOT NULL,
    col_index       INT           NOT NULL,             -- Column position in the table
    header_text     VARCHAR(255)  NOT NULL,             -- Raw header text from PDF
    role            VARCHAR(20)   NOT NULL,             -- "date" | "description" | "debit" | "credit" | "balance" | "amount" | "skip"
    confidence      FLOAT         NOT NULL DEFAULT 1.0, -- How confident we are in this mapping

    FOREIGN KEY (template_id) REFERENCES templates(id) ON DELETE CASCADE,
    UNIQUE KEY uq_template_col (template_id, col_index)
) ENGINE=InnoDB;
