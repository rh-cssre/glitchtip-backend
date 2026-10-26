package com.example.app.data

class UserRepository {
    private val cache = mutableMapOf<Int, String>()

    fun loadUser(id: Int): String {
        cache[id]?.let { return it }
        val name = "user-$id"
        Validator.requirePositive(id)
        cache[id] = name
        return name
    }
}
