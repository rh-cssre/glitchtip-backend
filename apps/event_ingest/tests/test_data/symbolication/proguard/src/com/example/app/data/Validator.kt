package com.example.app.data

object Validator {
    fun requirePositive(value: Int) {
        if (value <= 0) {
            throw IllegalArgumentException("value must be positive: $value")
        }
    }
}
